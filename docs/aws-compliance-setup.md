# AWS compliance setup: S3 Object Lock + CloudTrail

Manual, run-once provisioning for Week 2's audit hardening (mirrors how Week 1 had you
create the RDS instance and grant Bedrock access yourself). Two independent pieces:

1. **Audit export bucket** — where `cap audit export` writes hash-chained
   `screening_audit` batches, protected by S3 Object Lock in **COMPLIANCE** mode.
2. **CloudTrail trail** — AWS account-level API activity logging, with log file
   integrity validation enabled. Unrelated to the audit export bucket; kept in a
   separate bucket on purpose (see [Why a separate bucket](#why-a-separate-bucket)).

Account used below: `784137772067`, region `us-east-1` — substitute your own.

## 1. Audit export bucket (S3 Object Lock, COMPLIANCE mode)

Object Lock requires versioning to be enabled **before** it can be turned on, and
(for the default-retention configuration used here) Object Lock must be enabled
**at bucket creation time** — it cannot be added to an existing bucket later.

```bash
BUCKET=cap-audit-export-784137772067
REGION=us-east-1

# 1a. Create the bucket with Object Lock enabled from the start.
aws s3api create-bucket \
  --bucket "$BUCKET" \
  --region "$REGION" \
  --object-lock-enabled-for-bucket

# 1b. Versioning is required by Object Lock and is enabled automatically by
# --object-lock-enabled-for-bucket, but confirm it explicitly:
aws s3api put-bucket-versioning \
  --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled

# 1c. Set the bucket-level default retention: COMPLIANCE mode, N days.
# Print the exact command (uses the app's configured AUDIT_RETENTION_DAYS,
# so it never drifts from what the app assumes):
uv run cap audit bucket-config
# -> paste and run the printed `aws s3api put-object-lock-configuration ...` command
```

`cap audit bucket-config` reads `Settings.audit_retention_days` (env
`AUDIT_RETENTION_DAYS`, default **30 days / 1 month — a POC default**; raise this to
whatever your regulatory retention requirement is — e.g. multi-year for AML
recordkeeping — before using this for anything beyond a proof of concept) so the
bucket's retention always matches what the application config declares. Its output
looks like:

```bash
aws s3api put-object-lock-configuration --bucket cap-audit-export-784137772067 \
  --object-lock-configuration '{"ObjectLockEnabled": "Enabled", "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": 30}}}'
```

**COMPLIANCE vs. GOVERNANCE mode** (for reference — this project always uses
COMPLIANCE): COMPLIANCE mode means *nobody*, including the AWS account root user,
can shorten the retention period or delete a locked object before it expires — the
direct S3 equivalent of a Retention Lock **Compliance**-mode pool policy. GOVERNANCE
mode is the **Enterprise/Governance**-mode equivalent: overridable by a principal
holding `s3:BypassGovernanceRetention`. Because retention is set once at the
bucket level (not per object), the application's own IAM role never needs
`s3:PutObjectRetention` or `s3:BypassGovernanceRetention` — the write path
(`cap audit export`) is structurally incapable of shortening or bypassing retention.

### IAM policy for the exporter

Least-privilege: the role/user running `cap audit export` / `cap audit verify` needs
only read/write/list on the bucket's own prefix — never retention-management
permissions.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CapAuditExport",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": "arn:aws:s3:::cap-audit-export-784137772067/screening-audit/*"
    },
    {
      "Sid": "CapAuditExportList",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::cap-audit-export-784137772067",
      "Condition": { "StringLike": { "s3:prefix": "screening-audit/*" } }
    }
  ]
}
```

### App configuration

Add to `.env` (see `.env.example`):

```bash
AUDIT_EXPORT_BUCKET=cap-audit-export-784137772067
AUDIT_EXPORT_PREFIX=screening-audit
AUDIT_RETENTION_DAYS=30
```

### Running the exporter

```bash
uv run cap audit export   # push any screening_audit rows not yet exported
uv run cap audit verify   # re-derive the hash chain across every exported batch
```

`cap audit export` is not on the graph's hot path and is not scheduled by this
project — run it on demand or wire it into a cron job / scheduled task per your
environment. It is idempotent and resumable: running it with nothing new to export
is a no-op (`row_count: 0`).

## 2. CloudTrail: log file integrity validation

Pure AWS trail-level setting (`EnableLogFileValidation`) — no application code is
involved. CloudTrail needs its own bucket with a bucket policy granting the
`cloudtrail.amazonaws.com` service principal write access.

```bash
TRAIL_BUCKET=cap-cloudtrail-784137772067
TRAIL_NAME=cap-account-trail
ACCOUNT_ID=784137772067
REGION=us-east-1

# 2a. Create the CloudTrail bucket (plain — no Object Lock; CloudTrail manages
# its own log file integrity via the validation feature below, not Object Lock).
aws s3api create-bucket --bucket "$TRAIL_BUCKET" --region "$REGION"

# 2b. Attach the CloudTrail service-principal bucket policy.
cat > /tmp/cloudtrail-bucket-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AWSCloudTrailAclCheck",
      "Effect": "Allow",
      "Principal": { "Service": "cloudtrail.amazonaws.com" },
      "Action": "s3:GetBucketAcl",
      "Resource": "arn:aws:s3:::${TRAIL_BUCKET}"
    },
    {
      "Sid": "AWSCloudTrailWrite",
      "Effect": "Allow",
      "Principal": { "Service": "cloudtrail.amazonaws.com" },
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::${TRAIL_BUCKET}/AWSLogs/${ACCOUNT_ID}/*",
      "Condition": { "StringEquals": { "s3:x-amz-acl": "bucket-owner-full-control" } }
    }
  ]
}
EOF
aws s3api put-bucket-policy --bucket "$TRAIL_BUCKET" --policy file:///tmp/cloudtrail-bucket-policy.json

# 2c. Create the trail with log file validation enabled.
aws cloudtrail create-trail \
  --name "$TRAIL_NAME" \
  --s3-bucket-name "$TRAIL_BUCKET" \
  --enable-log-file-validation

aws cloudtrail start-logging --name "$TRAIL_NAME"

# 2d. Confirm.
aws cloudtrail get-trail-status --name "$TRAIL_NAME" \
  --query '{IsLogging: IsLogging, LatestDeliveryError: LatestDeliveryError}'
aws cloudtrail describe-trails --trail-name-list "$TRAIL_NAME" \
  --query 'trailList[0].LogFileValidationEnabled'
# -> expect: true
```

If the trail already exists and was created without validation:

```bash
aws cloudtrail update-trail --name "$TRAIL_NAME" --enable-log-file-validation
```

### Why a separate bucket

The audit export bucket's IAM/Object Lock posture (least-privilege app role,
COMPLIANCE-mode retention keyed to a compliance policy) is deliberately kept
independent of CloudTrail's bucket, which needs a broader service-principal
write grant and follows its own AWS-managed log file validation/digest scheme
rather than Object Lock. Mixing the two would couple two unrelated trust
boundaries for no benefit.

## Verification checklist

- [ ] `aws s3api get-object-lock-configuration --bucket $BUCKET` shows
      `"ObjectLockEnabled": "Enabled"` and the configured `DefaultRetention`.
- [ ] `uv run cap audit export` succeeds and returns a non-null `manifest_key`
      after at least one alert has been screened.
- [ ] `uv run cap audit verify` returns `{"ok": true, ...}`.
- [ ] Attempting to delete an exported object before its retention expires
      (`aws s3api delete-object --bucket $BUCKET --key <data_key>`) fails with
      an `AccessDenied` / `InvalidArgument` referencing Object Lock — confirms
      COMPLIANCE mode is actually enforced, not just configured.
- [ ] `aws cloudtrail describe-trails --trail-name-list $TRAIL_NAME --query
      'trailList[0].LogFileValidationEnabled'` returns `true`.
- [ ] `aws cloudtrail get-trail-status --name $TRAIL_NAME` shows `IsLogging: true`.
