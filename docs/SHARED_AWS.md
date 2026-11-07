# Reuse the existing AWS host

Status: approved architecture direction, not a newly deployed environment.

## Physical resources

Reuse the existing development EC2 host, VPC, subnet, encrypted EBS storage and
public HTTPS entry point in Canada Central. The starting host has 8 GiB RAM and
8 GiB disk. These are recorded configuration values, not a fresh capacity audit.
No additional EC2 instance, GPU, Kubernetes cluster or managed database is planned.
Keep model inference external. Resource reuse does not guarantee free capacity.

The existing prototype remains outside this project's development scope. Existing
applications must continue running when the new demo is deployed or rolled back.

## Application isolation on that host

Use a dedicated `preflight-oss` namespace for new API, MCP and PostgreSQL
containers, an internal network, a PostgreSQL volume, loopback bindings and secret
storage. Use `preflight_oss` as the new database with dedicated API/MCP read-only
roles. The default plan is separate database storage, not shared mutable tables.
Image layers may be reused; credentials, volumes and application limits must not be.

Measure existing and proposed memory, CPU, disk and model-request consumption
before starting a second stack. Set resource limits and cap public demo
concurrency. Sharing a host means sharing host-level failures and resource
contention; container separation does not provide separate-host isolation.

Do not copy old scripts that replace fixed `preflight-api`, `preflight-mcp` or
`preflight-postgres` names, or write to a fixed existing application directory.
New deployment tooling must accept explicit environment names, show a dry-run
change summary and refuse to overwrite resources outside its namespace.

## Public demo path

React on Cloudflare Workers Static Assets → a same-origin streaming Worker →
the new demo's HTTPS hostname on the existing Nginx host → its FastAPI container.

The Worker holds only the new demo's upstream access key. Copilot credentials stay
server-side on the EC2 host. The public demo keeps bounded anonymous access;
contributors use their own credentials when self-hosting. A frontend deployment
does not grant access to any other backend or credential.

A stable hostname and matching certificate are required. Workers cannot fetch a
literal IP address directly, so the existing IP-certificate URL is not a reusable
upstream URL for this new hosting adapter (confirmed on the deployed Worker on
November 6, 2026: the edge answered the subrequest 403 `text/plain` and the proxy
returned its fixed 502). The same evening the host gained a second Nginx server block
for a demo hostname with its own Let's Encrypt certificate, added through the existing
SSM access; the IP certificate, the two routes and the renewal timer are unchanged, and
`docs/BASELINE.md` records the steps. The hostname is not written in this repository. Select the demo hostname, configure DNS
and TLS, and test the real streaming path before adding a live link to the README.
Route only chat and its configuration-status endpoint through the public listener.
Keep database, MCP and raw record-list endpoints off that public route.

## Managed tool access

Hosted Copilot runtime → AgentCore Gateway → private HTTPS MCP target → PostgreSQL.

Use Gateway's managed VPC connectivity to reach an HTTPS listener for the new MCP
service on the existing host. A Docker-only service address is not reachable by
Gateway. Verify private routing, certificate trust, host validation, supported MCP
protocol, tool-name mapping and inbound/outbound authorization before changing
the live model path. Keep the current per-tool and SQL permission boundaries.

Gateway is an additional managed AWS service with its own IAM/network/credential
configuration and usage charges. It is not an EC2 instance. Reusing the server
does not authorize treating the new managed resources as already configured or
free. Record the expected charges and selected regional/subnet support in the
implementation plan. Prefer the existing region; resolve incompatibilities before provisioning.

Local self-hosting supports direct authenticated MCP without AWS. Hosted Gateway
failures must be visible; do not silently bypass Gateway authorization in the demo.

## Deployment acceptance

1. Record a read-only capacity/resource inventory without exposing identifiers.
2. Provision only the new namespaced services and configuration.
3. Check database constraints, tool-versus-SQL results and stream completion.
4. Check unauthenticated Gateway calls, invalid tools and public private-route access fail.
5. Verify one real model request through the new frontend and Gateway against SQL.
6. Verify the existing prototype's service health after deployment and rollback.
7. Record measured latency, resource use, charges/estimates and rollback commands.

No step in this deployment sequence was executed during workspace preparation.

## Sources checked November 5, 2026

- [Gateway private MCP connectivity](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-vpc-egress.html)
- [Gateway MCP target configuration](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-MCPservers.html)
- [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)
- [Workers static assets](https://developers.cloudflare.com/workers/static-assets/)
- [Workers streaming](https://developers.cloudflare.com/workers/runtime-apis/streams/)
- [Workers upstream IP restriction](https://developers.cloudflare.com/workers/platform/known-issues/#fetch-to-ip-addresses)
