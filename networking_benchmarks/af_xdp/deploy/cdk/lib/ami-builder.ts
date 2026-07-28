import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Tags, RemovalPolicy, Duration, CustomResource } from 'aws-cdk-lib';
import * as fs from 'fs';
import * as path from 'path';

export interface AmiBuilderStackProps extends cdk.StackProps {
  /** SSH key pair name for debugging access to the builder instance */
  keyPairName: string;
  /** Instance type for the builder. Should match target fleet type class. Default: c7i.xlarge */
  instanceType?: string;
  /** Git repository URL. Default: aws-samples/trading-latency-benchmark */
  gitRepo?: string;
  /** Git ref (branch/tag/commit) to build. Default: main */
  gitRef?: string;
}

/**
 * Launches a temporary EC2 instance that bakes an AMI with all AF_XDP
 * benchmark dependencies pre-installed. After the AMI is created, the
 * builder instance is terminated.
 *
 * Usage:
 *   cdk deploy --context deploymentType=ami-builder --context keyPairName=virginia
 *
 * Output:
 *   AmiId — use with: --context amiId=<output>
 */
export class AmiBuilderStack extends cdk.Stack {
  public readonly amiId: string;

  constructor(scope: cdk.App, id: string, props: AmiBuilderStackProps) {
    super(scope, id, props);

    const instanceType = props.instanceType ?? 'c7i.xlarge';
    const gitRepo = props.gitRepo ?? 'https://github.com/shaws256/trading-latency-benchmark.git';
    const gitRef = props.gitRef ?? 'test';

    // ── VPC (minimal) ────────────────────────────────────────────────────────
    const vpc = new ec2.Vpc(this, 'BuilderVpc', {
      natGateways: 0,
      maxAzs: 1,
      subnetConfiguration: [{
        cidrMask: 24,
        name: 'Public',
        subnetType: ec2.SubnetType.PUBLIC,
        mapPublicIpOnLaunch: true,
      }],
    });

    // ── Security Group ───────────────────────────────────────────────────────
    const sg = new ec2.SecurityGroup(this, 'BuilderSg', {
      vpc,
      description: 'AMI builder: SSH for debugging only',
      allowAllOutbound: true,
    });
    sg.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(22), 'SSH debug access');

    // ── IAM Role ─────────────────────────────────────────────────────────────
    const role = new iam.Role(this, 'BuilderRole', {
      assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore'),
      ],
    });
    // Allow instance to create AMI of itself
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['ec2:CreateImage', 'ec2:DescribeImages', 'ec2:CreateTags'],
      resources: ['*'],
    }));
    // Allow cfn-signal
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['cloudformation:SignalResource'],
      resources: [cdk.Arn.format({ service: 'cloudformation', resource: 'stack', resourceName: `${this.stackName}/*` }, this)],
    }));
    // Allow instance to stop itself
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['ec2:StopInstances'],
      resources: ['*'],
      conditions: { StringEquals: { 'ec2:ResourceTag/Name': 'af-xdp-ami-builder' } },
    }));
    // Allow pushing bake logs to CloudWatch
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: [cdk.Arn.format({ service: 'logs', resource: 'log-group', resourceName: '/af-xdp/ami-builder:*' }, this)],
    }));

    // ── Wait Condition Handle ──
    const waitHandle = new cdk.CfnWaitConditionHandle(this, 'AmiWaitHandle');

    // ── UserData ─────────────────────────────────────────────────────────────
    const bakeScript = fs.readFileSync(path.resolve(__dirname, '..', 'scripts', 'bake-ami.sh'), 'utf-8');

    const userData = ec2.UserData.forLinux();
    // Write env vars into the script header, then base64 encode + execute.
    // This ensures variables resolve inside the child process regardless of
    // how CDK's UserData wrapper handles export inheritance.
    const envBlock = [
      `export STACK_NAME="${this.stackName}"`,
      `export REGION="${this.region}"`,
      `export GIT_REPO="${gitRepo ?? 'https://github.com/shaws256/trading-latency-benchmark.git'}"`,
      `export GIT_REF="${gitRef ?? 'test'}"`,
    ].join('\n');
    // WAIT_HANDLE_URL contains a CFN Ref that must be resolved at deploy time,
    // so it can't be baked into the base64. Pass it as an argument instead.
    const fullScript = bakeScript.replace(
      'set -uo pipefail',
      `${envBlock}\nexport WAIT_HANDLE_URL="\$1"\nset -uo pipefail`
    );
    const bakeScriptB64 = Buffer.from(fullScript).toString('base64');

    userData.addCommands(
      `echo '${bakeScriptB64}' | base64 -d > /tmp/bake-ami.sh`,
      `chmod +x /tmp/bake-ami.sh`,
      `/tmp/bake-ami.sh "${waitHandle.ref}" || true`,
    );

    // ── Builder Instance ─────────────────────────────────────────────────────
    const instance = new ec2.Instance(this, 'Builder', {
      vpc,
      instanceType: new ec2.InstanceType(instanceType),
      machineImage: ec2.MachineImage.latestAmazonLinux2023(),
      securityGroup: sg,
      role,
      keyPair: ec2.KeyPair.fromKeyPairName(this, 'KeyPair', props.keyPairName),
      blockDevices: [{ deviceName: '/dev/xvda', volume: ec2.BlockDeviceVolume.ebs(30) }],
      userData,
    });
    instance.applyRemovalPolicy(RemovalPolicy.DESTROY);
    Tags.of(instance).add('Name', 'af-xdp-ami-builder');

    // ── Wait Condition (instance signals when bake is done) ───────────────────
    const waitCondition = new cdk.CfnWaitCondition(this, 'AmiWaitCondition', {
      handle: waitHandle.ref,
      timeout: '1200', // 20 minutes max
      count: 1,
    });
    waitCondition.node.addDependency(instance);

    // ── Lambda: Create AMI + terminate builder ───────────────────────────────
    const createAmiFunction = new lambda.Function(this, 'CreateAmiFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      timeout: Duration.minutes(15),
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: lambda.Code.fromInline(`
import boto3
import cfnresponse
import time

def handler(event, context):
    if event['RequestType'] == 'Delete':
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
        return

    ec2 = boto3.client('ec2')
    instance_id = event['ResourceProperties']['InstanceId']
    stack_name = event['ResourceProperties']['StackName']
    ami_id = None

    try:
        # Create AMI
        ts = time.strftime('%Y%m%d-%H%M%S')
        ami_name = f'af-xdp-bench-{ts}'
        print(f'Creating AMI {ami_name} from {instance_id}')
        resp = ec2.create_image(
            InstanceId=instance_id,
            Name=ami_name,
            Description=f'AF_XDP benchmark AMI built by {stack_name}',
            NoReboot=False,
            TagSpecifications=[{
                'ResourceType': 'image',
                'Tags': [
                    {'Key': 'Name', 'Value': ami_name},
                    {'Key': 'Builder', 'Value': stack_name},
                    {'Key': 'Source', 'Value': 'af-xdp-ami-builder'},
                ]
            }]
        )
        ami_id = resp['ImageId']
        print(f'AMI creation initiated: {ami_id}')

        # Wait for AMI to become available
        waiter = ec2.get_waiter('image_available')
        waiter.wait(ImageIds=[ami_id], WaiterConfig={'Delay': 15, 'MaxAttempts': 40})
        print(f'AMI available: {ami_id}')

        # Store AMI ID in SSM Parameter Store for FleetStack auto-resolution
        region = event['ResourceProperties'].get('Region', boto3.session.Session().region_name)
        ssm = boto3.client('ssm')
        ssm_param = f'/af-xdp/ami/{region}'
        ssm.put_parameter(Name=ssm_param, Value=ami_id, Type='String', Overwrite=True)
        print(f'SSM parameter written: {ssm_param} = {ami_id}')

        # Terminate builder instance
        ec2.terminate_instances(InstanceIds=[instance_id])
        print(f'Builder instance {instance_id} terminated')

        cfnresponse.send(event, context, cfnresponse.SUCCESS, {'AmiId': ami_id}, ami_id)

    except Exception as e:
        print(f'ERROR: {e}')
        # Terminate instance regardless
        try:
            ec2.terminate_instances(InstanceIds=[instance_id])
            print(f'Builder instance {instance_id} terminated (after error)')
        except Exception:
            pass
        # Deregister failed AMI if one was created
        if ami_id:
            try:
                ec2.deregister_image(ImageId=ami_id)
                print(f'Deregistered failed AMI {ami_id}')
            except Exception:
                pass
        cfnresponse.send(event, context, cfnresponse.FAILED, {'Error': str(e)})
`),
    });
    createAmiFunction.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'ec2:CreateImage', 'ec2:DescribeImages', 'ec2:CreateTags',
        'ec2:TerminateInstances', 'ec2:DescribeInstances',
        'ec2:DeregisterImage',
        'ssm:PutParameter',
      ],
      resources: ['*'],
    }));

    // ── Custom Resource: trigger AMI creation after wait condition ────────────
    const amiResource = new CustomResource(this, 'AmiResource', {
      serviceToken: createAmiFunction.functionArn,
      properties: {
        InstanceId: instance.instanceId,
        StackName: this.stackName,
        Region: this.region,
        // Force re-creation on each deploy
        Timestamp: Date.now().toString(),
      },
    });
    amiResource.node.addDependency(waitCondition);

    // ── Outputs ──────────────────────────────────────────────────────────────
    this.amiId = amiResource.getAttString('AmiId');

    new cdk.CfnOutput(this, 'AmiId', {
      value: this.amiId,
      description: 'Baked AMI ID — use with: --context amiId=<this-value>',
    });
    new cdk.CfnOutput(this, 'BuilderInstanceId', {
      value: instance.instanceId,
      description: 'Builder instance (terminated after AMI creation)',
    });
  }
}
