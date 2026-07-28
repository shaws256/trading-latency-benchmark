#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import * as fs from 'fs';
import * as path from 'path';
import { FleetStack, FleetEntry } from '../lib/fleet';
import { AmiBuilderStack } from '../lib/ami-builder';

const app = new cdk.App();

const keyPairName          = app.node.tryGetContext('keyPairName');
const region               = app.node.tryGetContext('region') || 'us-east-1';
const secondaryKeyPairName = app.node.tryGetContext('secondaryKeyPairName');
const amiId                = app.node.tryGetContext('amiId') || undefined;
const secondaryAmiId       = app.node.tryGetContext('secondaryAmiId') || undefined;
const vpcCidr              = app.node.tryGetContext('vpcCidr') || undefined;
const secondaryVpcCidr     = app.node.tryGetContext('secondaryVpcCidr') || undefined;
const dataPort             = app.node.tryGetContext('dataPort');

if (!keyPairName) {
  throw new Error('keyPairName context is required. Pass --context keyPairName=<name>');
}

// ── Fleet resolution ─────────────────────────────────────────────────────────
// Priority: --context fleet=... > --context scenario=...
//
// Formats:
//   --context fleet='[{"count":2}]'                        (inline JSON)
//   --context fleet=@path/to/file.json                     (load from file)
//   --context scenario=ucast/az-cpg-3x                     (from scenarios/)
//
function resolveFleet(): FleetEntry[] {
  const fleetRaw     = app.node.tryGetContext('fleet');
  const scenarioName = app.node.tryGetContext('scenario');

  let fleetData: any;

  if (fleetRaw) {
    if (typeof fleetRaw === 'object' && Array.isArray(fleetRaw)) {
      fleetData = fleetRaw;
    } else if (typeof fleetRaw === 'string') {
      if (fleetRaw.startsWith('@')) {
        const filePath = path.resolve(fleetRaw.slice(1));
        if (!fs.existsSync(filePath)) {
          throw new Error(`Fleet file not found: ${filePath}`);
        }
        fleetData = JSON.parse(fs.readFileSync(filePath, 'utf-8'));
      } else {
        fleetData = JSON.parse(fleetRaw);
      }
    }
  } else if (scenarioName) {
    const scenarioDir = path.resolve(__dirname, '..', 'scenarios');
    const scenarioFile = path.join(scenarioDir, `${scenarioName}.json`);
    if (!fs.existsSync(scenarioFile)) {
      // List available scenarios recursively
      const listScenarios = (dir: string, prefix = ''): string[] => {
        if (!fs.existsSync(dir)) return [];
        const entries = fs.readdirSync(dir, { withFileTypes: true });
        const results: string[] = [];
        for (const e of entries) {
          if (e.isDirectory()) {
            results.push(...listScenarios(path.join(dir, e.name), `${prefix}${e.name}/`));
          } else if (e.name.endsWith('.json')) {
            results.push(`${prefix}${e.name.replace('.json', '')}`);
          }
        }
        return results;
      };
      const available = listScenarios(scenarioDir);
      throw new Error(
        `Scenario not found: ${scenarioName}\n` +
        `Looked at: ${scenarioFile}\n` +
        (available.length > 0 ? `Available:\n  ${available.join('\n  ')}` : 'No scenarios found.')
      );
    }
    fleetData = JSON.parse(fs.readFileSync(scenarioFile, 'utf-8'));
  }

  if (!fleetData) {
    throw new Error(
      'Fleet spec required. Provide one of:\n' +
      '  --context scenario=ucast/az-cpg-3x      (from scenarios/)\n' +
      '  --context fleet=@path/to/file.json       (from file)\n' +
      '  --context fleet=\'[{"count":2}]\'          (inline JSON)'
    );
  }

  const fleet = Array.isArray(fleetData) ? fleetData : fleetData.fleet;
  if (!Array.isArray(fleet) || fleet.length === 0) {
    throw new Error('Fleet spec must be a non-empty JSON array');
  }
  // Filter out comment-only entries (objects with only _comment key)
  const filtered = fleet.filter((e: any) => !('_comment' in e && Object.keys(e).length === 1));
  if (filtered.length === 0) {
    throw new Error('Fleet spec has no valid entries (only comments found)');
  }
  return filtered as FleetEntry[];
}

// ── Deploy ───────────────────────────────────────────────────────────────────
const deploymentType = app.node.tryGetContext('deploymentType') || 'fleet';
const stackName = app.node.tryGetContext('stackName') || 'XdpStack';

switch (deploymentType) {
  case 'ami-builder': {
    const gitRepo = app.node.tryGetContext('gitRepo') || undefined;
    const gitRef = app.node.tryGetContext('gitRef') || undefined;
    const instanceType = app.node.tryGetContext('instanceType') || undefined;

    new AmiBuilderStack(app, `${stackName}-AmiBuilder`, {
      env: { account: process.env.CDK_DEFAULT_ACCOUNT, region },
      keyPairName,
      instanceType,
      gitRepo,
      gitRef,
    });
    break;
  }

  default: {
    const fleet = resolveFleet();

    new FleetStack(app, stackName, {
      env: { account: process.env.CDK_DEFAULT_ACCOUNT, region },
      keyPairName,
      secondaryKeyPairName,
      amiId,
      secondaryAmiId,
      vpcCidr,
      secondaryVpcCidr,
      dataPort: dataPort ? parseInt(dataPort, 10) : undefined,
      fleet,
    });
    break;
  }
}
