# Upgrading

## Chart 0.4.0: Stalwart 1.1.0, Bulwark 1.21.1

Stalwart 1.0.3 and 1.1.0:

- **Default role permissions:** on servers first installed before 1.1.0, the default roles lack the permissions added since 1.0.0, including those for symmetric keys (1.0.3) and passkeys (1.1.0). Install or upgrade once with `extraEnv: {STALWART_UPDATE_ROLE_PERMISSIONS: "true"}`, wait for the pods to restart, then remove the variable. This replaces the manual `x:Role/set` call in the 0.3.0 notes.
- **OAuth scopes:** apps other than the Stalwart WebUI that sign in through OAuth need the new scopes `urn:stalwart:oauth:scope:security`, `urn:stalwart:oauth:scope:settings` or `urn:stalwart:oauth:scope:full` to manage passwords, app passwords, API keys and account settings. `OAuthClient.firstParty` no longer exists. For Bulwark with `webmail.oauth`, set `webmail.oauth.extraScopes` and allow the scopes on its client, as described in the README's webmail section; without them, mail works after an OAuth sign-in, but Settings → Security and the admin console fail. Bulwark 1.21 requests the configured scopes correctly, also from providers such as Pocket ID ([bulwarkmail/webmail#1527](https://github.com/bulwarkmail/webmail/issues/1527)).
- **Passkeys (1.1.0):** accounts in the internal directory can sign in with passkeys on Stalwart's login page. The relying party is the host of Stalwart's public URL, so the WebUI must be served on that host or a subdomain of it.

Bulwark 1.21:

- Mail, calendars and contacts can be kept offline in an encrypted database on the device; admins cap or wipe it per device. The Bulwark desktop app (beta) needs 1.21 on the server for its desktop features.
- New settings, set through `webmail.extraEnv` (or Admin > Branding): login page layout and background (`LOGIN_LAYOUT`, `LOGIN_BACKGROUND_*`), `BROWSER_SESSION_SIGN_IN` (needs `webmail.sessionSecret`), `DESKTOP_BROWSER_SIGN_IN` and `ACCOUNT_MANAGEMENT_URL`.
- Undo send is on by default.

Chart changes:

- The webmail runs as UID/GID 1001, the image's own `nextjs` user, instead of 1000. Existing volumes need no step on storage that applies `fsGroup` (most block storage): Kubernetes changes the volume's group to 1001 on the next start. On storage that ignores `fsGroup`, such as many NFS setups, change the owner of the volume's files to 1001 first, or keep `webmail.podSecurityContext.runAsUser`, `runAsGroup` and `fsGroup` at 1000.
- New `networkPolicy` (off by default): ingress NetworkPolicies for the Stalwart and webmail pods. See the README's Security section.
- New `webmail.extraInitContainers`, `webmail.persistence.existingClaim` and `webmail.oauth.passwordFallback` (Bulwark's `OAUTH_PASSWORD_FALLBACK_ENABLED`).
- New webmail values for settings previously set through `webmail.extraEnv`: `trustedProxyDepth`, `logFormat`, `logLevel`, `oauth.endSession`, `oauth.endSessionUrl` and `oauth.postLogoutRedirectUri`. Variables in `webmail.extraEnv` and `webmail.extraEnvVars` now replace the chart's own instead of being added a second time, as for Stalwart.
- New `encryption.blobKey` and `blobStore.compression` for Stalwart 1.0.3's blob encryption and Zstandard compression. See the README's [Blob encryption and compression](../README.md#blob-encryption-and-compression). If the provisioning account is not a superuser, update the role permissions (above) first: older default administrator roles lack the permissions for symmetric keys and the `BlobRecode` task.

See the upstream release notes of Stalwart [1.0.3](https://github.com/stalwartlabs/stalwart/releases/tag/v1.0.3) and [1.1.0](https://github.com/stalwartlabs/stalwart/releases/tag/v1.1.0), and Bulwark [1.21.0](https://github.com/bulwarkmail/webmail/releases/tag/1.21.0) and [1.21.1](https://github.com/bulwarkmail/webmail/releases/tag/1.21.1).

## Chart 0.3.0: Stalwart 1.0.2, Bulwark 1.20.0

Chart changes:

- Replace `email.provision` with `encryption.provision`, and `email.encryptAtRest` with `encryption.atRestEnable`. The chart rejects the old `email` block. `encryptOnAppend` is now configured per account, outside this chart.
- Multiple webmail replicas now require `webmail.sharedStore` (Redis protocol); a shared volume is insufficient. Follow the [staged migration](../README.md#moving-an-existing-installation-onto-a-store) to import the old volume with one replica before scaling. Preserve the existing session secret.
- `webmail.persistence.size` defaults to 1Gi (was 2Gi). Keep the current PVC size on upgrades: Kubernetes cannot shrink it. Use the new `webmail.persistence.annotations` to retain the old PVC after migration.
- Bulwark has a writable `/app/.next/cache`. Startup and liveness use `/api/health/live`; readiness uses `/api/ready`, which checks the store without depending on Stalwart's availability.

For servers upgraded from an earlier Stalwart release:

1. **Re-enable encryption at rest per account.** The upgrade disables account encryption until the user or an administrator selects the account's public key again. New messages are not encrypted at rest in the meantime; existing keys and encrypted messages are preserved.
2. **Disable encryption at rest again if you had turned it off server-wide.** The new `Encryption.atRestEnable` defaults to true. Set `encryption.atRestEnable: false` with `encryption.provision: true`.
3. **Grant the administrator role the new permissions.** Existing roles do not gain the new permissions automatically, so the encryption administration sections stay hidden. Call `x:Authentication/get` with `"ids": ["singleton"]` and identify the System Administrator role in `defaultAdminRoleIds` using `x:Role/get`. Replace `ROLE_ID` below and apply this patch once through the management Service (`kubectl port-forward svc/<fullname>-management 8080:8080`):

   ```sh
   curl -u admin:PASSWORD -H 'Content-Type: application/json' http://localhost:8080/jmap/ -d '{
     "using": ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"],
     "methodCalls": [["x:Role/set", {"update": {"ROLE_ID": {
       "enabledPermissions/sysEncryptionGet": true,
       "enabledPermissions/sysEncryptionUpdate": true,
       "enabledPermissions/sysExternalPublicKeyGet": true,
       "enabledPermissions/sysExternalPublicKeyCreate": true,
       "enabledPermissions/sysExternalPublicKeyUpdate": true,
       "enabledPermissions/sysExternalPublicKeyDestroy": true,
       "enabledPermissions/sysExternalPublicKeyQuery": true,
       "enabledPermissions/sysSmimeTrustAnchorGet": true,
       "enabledPermissions/sysSmimeTrustAnchorCreate": true,
       "enabledPermissions/sysSmimeTrustAnchorUpdate": true,
       "enabledPermissions/sysSmimeTrustAnchorDestroy": true,
       "enabledPermissions/sysSmimeTrustAnchorQuery": true,
       "enabledPermissions/taskExternalPublicKeyMaintenance": true
     }}}, "0"]]
   }'
   ```

4. **Check custom provisioning plans for DKIM2.** The composite post-quantum `ML-DSA-44` + `Ed25519` DKIM2 algorithm was replaced by pure `ML-DSA-44`.

See the upstream [release notes](https://github.com/stalwartlabs/stalwart/releases/tag/v1.0.2) and the [encryption at rest documentation](https://stalw.art/docs/encryption/at-rest).

## Chart 0.2.1: Stalwart 1.0.1

- Metric names changed. Prometheus metrics now start with `stalwart_` and use unit and `_total` suffixes. Update your dashboards and alerts.
- `OTEL_*` environment variables no longer work. Configure OpenTelemetry with the `serviceName`, `serviceNamespace` and `resourceAttributes` settings instead.
- The chart's settings are unchanged.

## Stalwart 1.0

This chart targets Stalwart 1.0. It does not retain compatibility with older Stalwart releases.

Before upgrading a 0.16 installation:

1. Back up the database, blob store, configuration and Kubernetes Secrets. Test restoration.
2. Follow the upstream [migration instructions](https://github.com/stalwartlabs/stalwart-migrate) for the data and settings.
3. Rewrite custom provisioning plans for the 1.0 schema. See the upstream [schema changes](https://github.com/stalwartlabs/stalwart-migrate/blob/main/SCHEMA_CHANGES.md).
4. Review external-directory authentication. System accounts use Stalwart-generated app passwords; their consumers must adopt the resulting Secret values.
5. Test sign-in, sending and receiving mail before directing production traffic to the new installation.

Helm rollback does not revert the database or settings written by provisioning. Returning to an older application version requires its matching backup and configuration.

See [reconciliation](reconciliation.md) before changing system-account Secret ownership.
