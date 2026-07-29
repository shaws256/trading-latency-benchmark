import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as cr from 'aws-cdk-lib/custom-resources';
import {
  Vpc,
  SubnetType,
  Instance,
  InstanceType,
  MachineImage,
  SecurityGroup,
  Port,
  Peer,
  BlockDeviceVolume,
  CfnPlacementGroup,
  UserData,
  KeyPair,
} from 'aws-cdk-lib/aws-ec2';
import { Tags, RemovalPolicy } from 'aws-cdk-lib';

// c7i.2xlarge = 8 vCPU / 4 physical cores. Non-competing busy-polling needs a
// dedicated physical core each for: replicator poll thread, rtt sender,
// rtt receiver, plus one core for OS + NIC IRQs. c7i.xlarge (2 physical
// cores) forces these threads to share physical cores/HT siblings, injecting
// jitter. See bake-ami.sh CPU-isolation section (isolcpus=1-3, nosmt).
const DEFAULT_INSTANCE_TYPE = 'c7i.2xlarge';
/** SSM parameter path where the AMI builder stores the latest AMI ID per region */
const SSM_AMI_PREFIX = '/af-xdp/ami';
const DEFAULT_ROLE = 'replicator';

export type PlacementStrategy = 'cluster' | 'spread' | 'partition';

/** A single node in the fleet specification. */
export interface FleetEntry {
  /** EC2 instance type. Default: c7i.2xlarge */
  type?: string;
  /** Number of instances. Default: 1 */
  count?: number;
  /** Logical role: "source", "replicator", "destination". Default: "replicator" */
  role?: string;
  /** AZ suffix (e.g. "a") or full AZ name (e.g. "us-east-1a"). Default: first AZ of region. */
  az?: string;
  /** Placement strategy: "cluster", "spread", "partition". Default: none. */
  pgType?: PlacementStrategy;
  /** Placement group name. Entries with the same name share a PG.
   *  Different names → separate PGs (even in the same AZ/strategy).
   *  Default: one shared PG per strategy+AZ combination. */
  pgName?: string;
  /** AWS region (e.g. "us-east-1", "eu-west-2"). Default: stack's region.
   *  Cross-region entries create a secondary VPC with VPC peering. */
  region?: string;
}

export interface FleetStackProps extends cdk.StackProps {
  /** SSH key pair name (must exist in the primary region) */
  keyPairName: string;
  /** SSH key pair name for secondary region (cross-region only). Defaults to keyPairName. */
  secondaryKeyPairName?: string;
  /** Custom AMI ID for primary region. Default: latest Amazon Linux 2023 */
  amiId?: string;
  /** Custom AMI ID for secondary region. Default: latest Amazon Linux 2023 */
  secondaryAmiId?: string;
  /** Primary VPC CIDR. Default: 10.61.0.0/16 */
  vpcCidr?: string;
  /** Secondary VPC CIDR (cross-region). Default: 10.62.0.0/16 */
  secondaryVpcCidr?: string;
  /** UDP data port (used for cross-region SG rules). Default: 5000 */
  dataPort?: number;
  /** Fleet specification (required). */
  fleet: FleetEntry[];
}

/**
 * Unified regional deployment stack.
 *
 * Handles single-region (same/cross-AZ) and cross-region topologies from
 * a single fleet spec. Cross-region is triggered when any FleetEntry has a
 * `region` field different from the stack's primary region.
 *
 * Cross-region creates:
 *   - A secondary VPC in the secondary region (via AwsCustomResource)
 *   - VPC peering connection between primary and secondary
 *   - Routes and SG rules on both sides for UDP data traffic
 *   - Instances in the secondary region
 */
export class FleetStack extends cdk.Stack {
  constructor(scope: cdk.App, id: string, props: FleetStackProps) {
    super(scope, id, props);

    const primaryRegion = this.region;
    const dataPort = props.dataPort ?? 5000;
    const primaryVpcCidr = props.vpcCidr ?? '10.61.0.0/16';
    const secondaryVpcCidr = props.secondaryVpcCidr ?? '10.62.0.0/16';

    // ── Partition fleet into primary vs secondary region ──────────────────
    const primaryEntries: (FleetEntry & { _resolvedAz: string })[] = [];
    const secondaryEntries: (FleetEntry & { _resolvedAz: string })[] = [];
    let secondaryRegion: string | undefined;

    for (const entry of props.fleet) {
      const entryRegion = entry.region ?? primaryRegion;
      const isSecondary = entryRegion !== primaryRegion;

      if (isSecondary) {
        if (secondaryRegion && secondaryRegion !== entryRegion) {
          throw new Error(
            `Only one secondary region is supported. Got entries for both "${secondaryRegion}" and "${entryRegion}".`
          );
        }
        secondaryRegion = entryRegion;
      }

      const resolveAz = (azSpec?: string, region?: string): string => {
        const reg = region ?? primaryRegion;
        if (!azSpec) return `${reg}a`; // default to 'a' suffix
        if (azSpec.includes('-')) return azSpec;
        return `${reg}${azSpec}`;
      };

      const resolved = { ...entry, _resolvedAz: resolveAz(entry.az, entryRegion) };
      if (isSecondary) {
        secondaryEntries.push(resolved);
      } else {
        primaryEntries.push(resolved);
      }
    }

    // ── Validate ─────────────────────────────────────────────────────────
    this.validateEntries(primaryEntries);
    if (secondaryEntries.length > 0) {
      this.validateEntries(secondaryEntries);
    }

    // ── Primary region AZs ───────────────────────────────────────────────
    const primaryAZs = Array.from(new Set(primaryEntries.map(e => e._resolvedAz))).sort();
    if (primaryAZs.length === 0) {
      throw new Error('Fleet must have at least one entry in the primary region');
    }

    // ── Primary VPC ──────────────────────────────────────────────────────
    const vpc = new Vpc(this, 'Vpc', {
      ipAddresses: ec2.IpAddresses.cidr(primaryVpcCidr),
      natGateways: 0,
      availabilityZones: primaryAZs,
      subnetConfiguration: [{
        cidrMask: 24,
        name: 'Public',
        subnetType: SubnetType.PUBLIC,
        mapPublicIpOnLaunch: true,
      }],
      gatewayEndpoints: {},
    });
    vpc.applyRemovalPolicy(RemovalPolicy.DESTROY);

    // ── Primary Security Group ───────────────────────────────────────────
    const sg = new SecurityGroup(this, 'Sg', {
      vpc,
      description: 'AF_XDP benchmark: SSH + intra-group + cross-region data',
      allowAllOutbound: true,
    });
    sg.applyRemovalPolicy(RemovalPolicy.DESTROY);
    sg.addIngressRule(Peer.anyIpv4(), Port.tcp(22), 'SSH');
    sg.addIngressRule(sg, Port.allTraffic(), 'All intra-group traffic');

    // ── Placement Groups (primary region) ────────────────────────────────
    const placementGroups = new Map<string, CfnPlacementGroup>();
    const getOrCreatePG = (strategy: PlacementStrategy, az: string, groupName?: string): CfnPlacementGroup => {
      // Key includes groupName so different named groups get separate PGs
      const key = groupName
        ? `${strategy}:${groupName}`
        : (strategy === 'cluster' ? `cluster:${az}` : strategy);

      if (!placementGroups.has(key)) {
        const sanitized = (groupName ?? az).replace(/[^a-zA-Z0-9]/g, '');
        const pgId = groupName
          ? `PG-${strategy}-${sanitized}`
          : (strategy === 'cluster' ? `PG-cluster-${sanitized}` : `PG-${strategy}`);
        const pg = new CfnPlacementGroup(this, pgId, { strategy });
        pg.applyRemovalPolicy(RemovalPolicy.DESTROY);
        placementGroups.set(key, pg);
      }
      return placementGroups.get(key)!;
    };

    // ── AMI + Key ────────────────────────────────────────────────────────
    // ── AMI resolution (priority: explicit amiId > SSM param > AL2023) ─────
    // SSM /af-xdp/ami/<region> is written by ami-builder after each bake.
    // Override with --context amiId=<id> for manual control.
    const resolveAmi = (region: string, explicitAmiId?: string): ec2.IMachineImage => {
      if (explicitAmiId) {
        return MachineImage.genericLinux({ [region]: explicitAmiId });
      }
      // SSM deploy-time resolution only works for the stack's own region
      if (region === primaryRegion) {
        const ssmParam = ssm.StringParameter.valueForStringParameter(this, `/af-xdp/ami/${region}`);
        return MachineImage.genericLinux({ [region]: ssmParam });
      }
      // Secondary region: fall back to AL2023 (build AMI there separately if needed)
      return MachineImage.latestAmazonLinux2023();
    };

    const primaryAmi = resolveAmi(primaryRegion, props.amiId);
    const keyPair = KeyPair.fromKeyPairName(this, 'KeyPair', props.keyPairName);

    // ── Deploy primary instances ─────────────────────────────────────────
    let globalIndex = 0;
    const fleetManifest: {
      index: number; instanceType: string; role: string;
      az: string; region: string; pgType: string | null; pgName: string | null; outputPrefix: string;
    }[] = [];

    for (const entry of primaryEntries) {
      const count = entry.count ?? 1;
      const role = entry.role ?? DEFAULT_ROLE;
      const instType = entry.type ?? DEFAULT_INSTANCE_TYPE;
      const az = entry._resolvedAz;
      const pgType = entry.pgType ?? null;

      for (let i = 0; i < count; i++) {
        const shortType = instType.replace('.', '-');
        const nodeId = `Node${globalIndex}`;
        const nodeName = `${role}-${globalIndex}-${shortType}`;
        const prefix = `Node${globalIndex}`;

        const inst = new Instance(this, nodeId, {
          vpc,
          instanceType: new InstanceType(instType),
          machineImage: primaryAmi,
          securityGroup: sg,
          vpcSubnets: { availabilityZones: [az] },
          keyPair,
          blockDevices: [{ deviceName: '/dev/xvda', volume: BlockDeviceVolume.ebs(100) }],
          userData: UserData.forLinux(),
        });
        inst.applyRemovalPolicy(RemovalPolicy.DESTROY);
        // Disable source/dest check for GRE/replication traffic
        (inst.node.defaultChild as ec2.CfnInstance).sourceDestCheck = false;

        if (pgType) {
          const pg = getOrCreatePG(pgType, az, entry.pgName);
          (inst.node.defaultChild as ec2.CfnInstance).placementGroupName = pg.ref;
          Tags.of(inst).add('PlacementStrategy', pgType);
          if (entry.pgName) Tags.of(inst).add('PlacementGroup', entry.pgName);
        }

        Tags.of(inst).add('Name', nodeName);
        Tags.of(inst).add('Role', role);
        Tags.of(inst).add('InstanceType', instType);
        Tags.of(inst).add('AZ', az);

        new cdk.CfnOutput(this, `${prefix}InstanceId`, { value: inst.instanceId });
        new cdk.CfnOutput(this, `${prefix}PublicIp`, { value: inst.instancePublicIp });
        new cdk.CfnOutput(this, `${prefix}PrivateIp`, { value: inst.instancePrivateIp });

        fleetManifest.push({
          index: globalIndex, instanceType: instType, role, az,
          region: primaryRegion, pgType, pgName: entry.pgName ?? null, outputPrefix: prefix,
        });
        globalIndex++;
      }
    }

    // ── Cross-region: secondary VPC + peering + instances ────────────────
    if (secondaryRegion && secondaryEntries.length > 0) {
      const secondaryAZs = Array.from(new Set(secondaryEntries.map(e => e._resolvedAz))).sort();

      // Secondary VPC (same account, different region)
      const secVpc = new Vpc(this, 'SecVpc', {
        ipAddresses: ec2.IpAddresses.cidr(secondaryVpcCidr),
        natGateways: 0,
        availabilityZones: secondaryAZs,
        subnetConfiguration: [{
          cidrMask: 24,
          name: 'Public',
          subnetType: SubnetType.PUBLIC,
          mapPublicIpOnLaunch: true,
        }],
        gatewayEndpoints: {},
      });
      secVpc.applyRemovalPolicy(RemovalPolicy.DESTROY);

      // Secondary SG
      const secSg = new SecurityGroup(this, 'SecSg', {
        vpc: secVpc,
        description: 'AF_XDP benchmark (secondary): SSH + intra-group + cross-region data',
        allowAllOutbound: true,
      });
      secSg.applyRemovalPolicy(RemovalPolicy.DESTROY);
      secSg.addIngressRule(Peer.anyIpv4(), Port.tcp(22), 'SSH');
      secSg.addIngressRule(secSg, Port.allTraffic(), 'All intra-group traffic');
      // Allow UDP data from primary VPC CIDR (will traverse peering)
      secSg.addIngressRule(Peer.ipv4(primaryVpcCidr), Port.udp(dataPort), 'UDP data from primary via peering');
      secSg.addIngressRule(Peer.ipv4(primaryVpcCidr), Port.tcp(12345), 'Control from primary via peering');

      // Primary SG: allow UDP from secondary VPC CIDR
      sg.addIngressRule(Peer.ipv4(secondaryVpcCidr), Port.udp(dataPort), 'UDP data from secondary via peering');
      sg.addIngressRule(Peer.ipv4(secondaryVpcCidr), Port.tcp(12345), 'Control from secondary via peering');

      // VPC Peering (same-account cross-region, auto-accepted)
      const peering = new ec2.CfnVPCPeeringConnection(this, 'VpcPeering', {
        vpcId: vpc.vpcId,
        peerVpcId: secVpc.vpcId,
        peerRegion: secondaryRegion,
        tags: [{ key: 'Name', value: 'PrimaryToSecondaryPeering' }],
      });

      // Routes: primary → secondary
      for (const subnet of vpc.publicSubnets) {
        new ec2.CfnRoute(this, `PriRoute${subnet.node.id}`, {
          routeTableId: subnet.routeTable.routeTableId,
          destinationCidrBlock: secondaryVpcCidr,
          vpcPeeringConnectionId: peering.ref,
        });
      }

      // Routes: secondary → primary
      for (const subnet of secVpc.publicSubnets) {
        new ec2.CfnRoute(this, `SecRoute${subnet.node.id}`, {
          routeTableId: subnet.routeTable.routeTableId,
          destinationCidrBlock: primaryVpcCidr,
          vpcPeeringConnectionId: peering.ref,
        });
      }

      // Secondary instances
      const secAmi = resolveAmi(secondaryRegion, props.secondaryAmiId);
      const secKeyPair = KeyPair.fromKeyPairName(this, 'SecKeyPair', props.secondaryKeyPairName ?? props.keyPairName);

      for (const entry of secondaryEntries) {
        const count = entry.count ?? 1;
        const role = entry.role ?? DEFAULT_ROLE;
        const instType = entry.type ?? DEFAULT_INSTANCE_TYPE;
        const az = entry._resolvedAz;
        const pgType = entry.pgType ?? null;

        for (let i = 0; i < count; i++) {
          const shortType = instType.replace('.', '-');
          const nodeId = `Node${globalIndex}`;
          const nodeName = `${role}-${globalIndex}-${shortType}`;
          const prefix = `Node${globalIndex}`;

          const inst = new Instance(this, nodeId, {
            vpc: secVpc,
            instanceType: new InstanceType(instType),
            machineImage: secAmi,
            securityGroup: secSg,
            vpcSubnets: { availabilityZones: [az] },
            keyPair: secKeyPair,
            blockDevices: [{ deviceName: '/dev/xvda', volume: BlockDeviceVolume.ebs(100) }],
            userData: UserData.forLinux(),
          });
          inst.applyRemovalPolicy(RemovalPolicy.DESTROY);
          (inst.node.defaultChild as ec2.CfnInstance).sourceDestCheck = false;

          if (pgType) {
            const pg = getOrCreatePG(pgType, az, entry.pgName);
            (inst.node.defaultChild as ec2.CfnInstance).placementGroupName = pg.ref;
            Tags.of(inst).add('PlacementStrategy', pgType);
            if (entry.pgName) Tags.of(inst).add('PlacementGroup', entry.pgName);
          }

          Tags.of(inst).add('Name', nodeName);
          Tags.of(inst).add('Role', role);
          Tags.of(inst).add('InstanceType', instType);
          Tags.of(inst).add('AZ', az);
          Tags.of(inst).add('Region', secondaryRegion);

          new cdk.CfnOutput(this, `${prefix}InstanceId`, { value: inst.instanceId });
          new cdk.CfnOutput(this, `${prefix}PublicIp`, { value: inst.instancePublicIp });
          new cdk.CfnOutput(this, `${prefix}PrivateIp`, { value: inst.instancePrivateIp });

          fleetManifest.push({
            index: globalIndex, instanceType: instType, role, az,
            region: secondaryRegion, pgType, pgName: entry.pgName ?? null, outputPrefix: prefix,
          });
          globalIndex++;
        }
      }

      new cdk.CfnOutput(this, 'PeeringConnectionId', { value: peering.ref });
      new cdk.CfnOutput(this, 'SecondaryVpcId', { value: secVpc.vpcId });
      new cdk.CfnOutput(this, 'SecondaryRegion', { value: secondaryRegion });
    }

    // ── Fleet outputs ────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'FleetManifest', {
      value: JSON.stringify(fleetManifest),
      description: 'JSON fleet manifest',
    });
    new cdk.CfnOutput(this, 'FleetSize', { value: String(globalIndex) });
    new cdk.CfnOutput(this, 'VpcId', { value: vpc.vpcId });
    new cdk.CfnOutput(this, 'AvailabilityZones', { value: primaryAZs.join(',') });
    if (placementGroups.size > 0) {
      new cdk.CfnOutput(this, 'PlacementGroups', {
        value: JSON.stringify(Array.from(placementGroups.keys())),
      });
    }
  }

  // ── Validation ─────────────────────────────────────────────────────────
  private validateEntries(entries: (FleetEntry & { _resolvedAz: string })[]): void {
    // Cluster: entries sharing the same pgName must be in one AZ
    const clusterGroupAZs = new Map<string, Set<string>>(); // groupName → AZs
    const spreadPerAz = new Map<string, number>();

    for (const entry of entries) {
      const count = entry.count ?? 1;
      const az = entry._resolvedAz;

      if (entry.pgType === 'cluster') {
        const group = entry.pgName ?? '__default__';
        if (!clusterGroupAZs.has(group)) clusterGroupAZs.set(group, new Set());
        clusterGroupAZs.get(group)!.add(az);
      }
      if (entry.pgType === 'spread') {
        spreadPerAz.set(az, (spreadPerAz.get(az) ?? 0) + count);
      }
    }

    for (const [group, azs] of clusterGroupAZs) {
      if (azs.size > 1) {
        const label = group === '__default__' ? '(unnamed)' : `"${group}"`;
        throw new Error(
          `Cluster placement group ${label} requires all instances in the same AZ. ` +
          `Got: ${Array.from(azs).join(', ')}`
        );
      }
    }

    for (const [az, count] of spreadPerAz) {
      if (count > 7) {
        throw new Error(`Spread placement max 7 per AZ. Got ${count} in ${az}.`);
      }
    }
  }
}
