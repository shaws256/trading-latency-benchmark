import * as cdk from 'aws-cdk-lib';
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

/** A single entry in the heterogeneous fleet specification. */
export interface FleetEntry {
  /** EC2 instance type, e.g. "c7i.4xlarge" */
  type: string;
  /** Number of instances of this type. Default: 1 */
  count?: number;
}

export interface SingleRegionStackProps extends cdk.StackProps {
  /** SSH key pair name (must exist in the target region) */
  keyPairName: string;

  // ── Simple mode (backward-compatible) ───────────────────────────────────
  /** EC2 instance type for all nodes. Default: c7i.4xlarge. Ignored if fleet is set. */
  instanceType?: string;
  /** Custom AMI ID. Default: latest Amazon Linux 2023 */
  amiId?: string;
  /** Number of subscriber instances. Default: 1. Ignored if fleet is set. */
  subscriberCount?: number;

  // ── Fleet mode (new — for matrix/heterogeneous deployments) ─────────────
  /**
   * Heterogeneous fleet specification. When provided, overrides instanceType
   * and subscriberCount. All nodes are peers (no exchange/feeder/subscriber
   * role distinction). Mixing instance families forces ZPM to place on
   * different host platforms → different racks → reveals intra-CPG variance.
   *
   * Example: [{"type":"c7i.4xlarge","count":2},{"type":"c6in.4xlarge","count":2}]
   */
  fleet?: FleetEntry[];
}

/**
 * Deploys EC2 instances inside a single Cluster Placement Group in a
 * single-AZ public VPC.
 *
 * Two modes:
 *   1. Simple (legacy): exchange + feeder + N subscribers, single instance type.
 *   2. Fleet: arbitrary mix of instance types, all peers. Better for matrix
 *      benchmarks and intra-CPG variability analysis.
 *
 * Deploy (simple):
 *   cdk deploy --context keyPairName=my-key --context instanceType=c7i.4xlarge
 *
 * Deploy (fleet):
 *   cdk deploy --context keyPairName=my-key \
 *     --context fleet='[{"type":"c7i.4xlarge","count":2},{"type":"c6in.4xlarge","count":2}]'
 */
export class SingleRegionStack extends cdk.Stack {
  constructor(scope: cdk.App, id: string, props: SingleRegionStackProps) {
    super(scope, id, props);

    // Use first AZ of the deployed region — avoids hardcoding a region-specific AZ.
    const az = this.availabilityZones[0];

    // ── VPC ──────────────────────────────────────────────────────────────────
    const vpc = new Vpc(this, 'Vpc', {
      natGateways: 0,
      availabilityZones: [az],
      subnetConfiguration: [
        {
          cidrMask: 24,
          name: 'Public',
          subnetType: SubnetType.PUBLIC,
          mapPublicIpOnLaunch: true,
        },
      ],
      gatewayEndpoints: {},
    });
    vpc.applyRemovalPolicy(RemovalPolicy.DESTROY);

    for (const subnet of vpc.publicSubnets) {
      if (subnet.node.defaultChild instanceof cdk.CfnResource) {
        (subnet.node.defaultChild as cdk.CfnResource).applyRemovalPolicy(RemovalPolicy.DESTROY);
      }
    }
    for (const child of vpc.node.findAll()) {
      if (child.node.defaultChild instanceof cdk.aws_ec2.CfnRouteTable) {
        (child.node.defaultChild as cdk.CfnResource).applyRemovalPolicy(RemovalPolicy.DESTROY);
      }
      if (child.node.defaultChild instanceof cdk.aws_ec2.CfnVPCEndpoint) {
        (child.node.defaultChild as cdk.CfnResource).applyRemovalPolicy(RemovalPolicy.DESTROY);
      }
    }

    // ── Security group ────────────────────────────────────────────────────────
    const sg = new SecurityGroup(this, 'Sg', {
      vpc,
      description: 'CPG: SSH + intra-group traffic',
      allowAllOutbound: true,
    });
    sg.applyRemovalPolicy(RemovalPolicy.DESTROY);
    sg.addIngressRule(Peer.anyIpv4(), Port.tcp(22), 'SSH from internet');
    sg.addIngressRule(sg, Port.allTraffic(), 'All traffic within group');

    // ── Cluster Placement Group ───────────────────────────────────────────────
    const pg = new CfnPlacementGroup(this, 'PlacementGroup', {
      strategy: 'cluster',
    });
    pg.applyRemovalPolicy(RemovalPolicy.DESTROY);

    // ── AMI + key pair ────────────────────────────────────────────────────────
    const ami = props.amiId
      ? MachineImage.genericLinux({ [this.region]: props.amiId })
      : MachineImage.latestAmazonLinux2023();

    const keyPair = KeyPair.fromKeyPairName(this, 'KeyPair', props.keyPairName);
    const vpcSubnets = { availabilityZones: [az] };

    // ── Helper: create an instance ────────────────────────────────────────────
    const mkInstance = (id: string, name: string, role: string, instType: string) => {
      const inst = new Instance(this, id, {
        vpc,
        instanceType: new InstanceType(instType),
        machineImage: ami,
        securityGroup: sg,
        vpcSubnets,
        keyPair,
        blockDevices: [{ deviceName: '/dev/xvda', volume: BlockDeviceVolume.ebs(100) }],
        userData: UserData.forLinux(),
      });
      inst.applyRemovalPolicy(RemovalPolicy.DESTROY);
      (inst.node.defaultChild as cdk.aws_ec2.CfnInstance).placementGroupName = pg.ref;
      Tags.of(inst).add('Name', name);
      Tags.of(inst).add('Role', role);
      Tags.of(inst).add('InstanceType', instType);
      Tags.of(inst).add('PlacementGroup', pg.ref);
      return inst;
    };

    const addOutputs = (inst: Instance, prefix: string, description: string) => {
      new cdk.CfnOutput(this, `${prefix}InstanceId`, { value: inst.instanceId,        description: `${description} instance ID` });
      new cdk.CfnOutput(this, `${prefix}PublicIp`,   { value: inst.instancePublicIp,   description: `${description} public IP` });
      new cdk.CfnOutput(this, `${prefix}PrivateIp`,  { value: inst.instancePrivateIp,  description: `${description} private IP` });
    };

    // ── Deploy instances ──────────────────────────────────────────────────────
    if (props.fleet && props.fleet.length > 0) {
      // Fleet mode: all nodes are peers, heterogeneous types
      let globalIndex = 0;
      const fleetManifest: { index: number; instanceType: string; outputPrefix: string }[] = [];

      for (const entry of props.fleet) {
        const count = entry.count ?? 1;
        for (let i = 0; i < count; i++) {
          const shortType = entry.type.replace('.', '-');
          const nodeId = `Node${globalIndex}`;
          const nodeName = `node-${globalIndex}-${shortType}`;
          const inst = mkInstance(nodeId, nodeName, 'matrix-node', entry.type);
          const prefix = `Node${globalIndex}`;
          addOutputs(inst, prefix, `Node ${globalIndex} (${entry.type})`);
          fleetManifest.push({ index: globalIndex, instanceType: entry.type, outputPrefix: prefix });
          globalIndex++;
        }
      }

      // Export fleet manifest as a JSON output for scripts to consume
      new cdk.CfnOutput(this, 'FleetManifest', {
        value: JSON.stringify(fleetManifest),
        description: 'JSON fleet manifest: [{index, instanceType, outputPrefix}]',
      });

      new cdk.CfnOutput(this, 'FleetSize', {
        value: String(globalIndex),
        description: 'Total number of instances in the fleet',
      });

    } else {
      // Simple mode (backward-compatible): exchange + feeder + N subscribers
      const instanceTypeStr = props.instanceType ?? 'c7i.4xlarge';
      const subscriberCount = props.subscriberCount ?? 1;

      addOutputs(mkInstance('ExchangeInstance', 'Trading-Exchange', 'exchange', instanceTypeStr), 'Exchange', 'Exchange');
      addOutputs(mkInstance('FeederInstance',   'Trading-Feeder',   'feeder',   instanceTypeStr), 'Feeder',   'Feeder');

      for (let i = 1; i <= subscriberCount; i++) {
        addOutputs(
          mkInstance(`Subscriber${i}Instance`, `Trading-Subscriber-${i}`, 'subscriber', instanceTypeStr),
          `Subscriber${i}`,
          `Subscriber ${i}`,
        );
      }
    }

    // ── Common outputs ────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'PlacementGroupName', {
      value: pg.ref,
      description: 'Cluster placement group name',
    });
    new cdk.CfnOutput(this, 'VpcId', {
      value: vpc.vpcId,
      description: 'VPC ID',
    });
    new cdk.CfnOutput(this, 'AvailabilityZone', {
      value: az,
      description: 'AZ all instances are placed in',
    });
  }
}
