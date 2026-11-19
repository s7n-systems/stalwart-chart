"""Finishes each install and upgrade after the provisioning Job: restarts Stalwart when its
settings changed, then publishes the app passwords of the system accounts to Secrets.

Restart: Stalwart reads its settings and binds its listeners at startup. A ReloadSettings
action would also validate the OIDC directory against the identity provider, which may not
be reachable yet; a restart does not. The pods roll when the hash of the settings plan differs
from the one on the StatefulSet's pod template, or from a marker kept in Stalwart's own
database (a role without permissions, described "managed:<release>/settings <state> <stamp>"):
after a database reset the provisioning Job writes the settings again, but the running pods
only read them after a restart. Before patching, the marker records the rollout as "pending"
with its stamp (hash@time); once the pods are ready, as "done". A retry that finds a pending
rollout of the current plan patches the same stamp again, which is a no-op, and only waits.

System accounts: Stalwart generates an app password's secret and returns it once, so the
Secret is the only copy. A new app password is created only when the one recorded in the
Secret is gone from Stalwart (first run, database reset, revoked), never because a login
failed. Stalwart reuses the ids of deleted app passwords, so a credential is identified by a
random reference in its description ("managed:<release>/<id> #<reference>"), which the Secret
records in an annotation. Credentials that carry the marker but are not recorded, left by a
run that died after creating one, are revoked. A lock annotation on the Secret, written with
optimistic concurrency, serializes runs of different Jobs. Applications that read the Secret
pick up a new password when they restart.

Blob encryption key: Stalwart (1.0.3 to 1.1.0) does not resolve plan references in Email and
FileStorage.blobEncryptionKey, so the provisioning Job only creates the SymmetricKey, and this
Job points both settings at it by its id (encryption.blobKey.email / fileStorage), or clears a
setting that points at it when disabled. A change restarts Stalwart, which loads keys at startup.

Administrator account (adminAccount, `reconcile.py bootstrap`, a Job before the provisioning
Job): the Jobs log in with an app password of a Stalwart account with the Admin role, accepted
only from 127.0.0.1 and ::1, so only through the gateway in the Stalwart pod. Nothing is done
while the recorded password works. Otherwise (first run, database reset, revoked password) the
Job writes a random recovery administrator (STALWART_RECOVERY_ADMIN) into the account's Secret,
restarts Stalwart, creates the account and its app password with it, removes the recovery
administrator again and restarts once more. The regular run then publishes further app
passwords of the account (adminAccount.clients), like those of the system accounts.

Errors fail the run, which the Job retries; every step can resume. Standard library only.
Configuration: the JSON file in $CONFIG_FILE; the Stalwart administrator in $STALWART_USER /
$STALWART_PASSWORD (not used by the bootstrap); the Kubernetes API through the projected service
account token.
"""
import base64
import ipaddress
import json
import os
import secrets
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request

PREFIX = "stalwart.s7n.systems/"
SETTINGS = PREFIX + "settings"
CREDENTIAL = PREFIX + "credential"
LOCK = PREFIX + "lock"
BOOTSTRAP = PREFIX + "admin-bootstrap"
RECOVERY_KEY = "STALWART_RECOVERY_ADMIN"
JMAP_USING = ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"]


class Failure(Exception):
    """An error that fails the run; the Job retries it."""


def log(scope, message):
    print(f"[{scope}] {message}", flush=True)


# --- Kubernetes ---------------------------------------------------------------------------

class Kubernetes:
    def __init__(self):
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if ":" in host:  # an IPv6 service address goes in brackets
            host = f"[{host}]"
        self.base = os.environ.get("KUBERNETES_API") or f"https://{host}:{port}"
        self.token_file = os.environ.get("KUBERNETES_TOKEN_FILE", "/var/run/secrets/stalwart/token")
        self.context = ssl.create_default_context(cafile=os.environ.get("KUBERNETES_CA_FILE", "/var/run/secrets/stalwart/ca.crt"))

    def request(self, method, path, body=None, content_type="application/json"):
        with open(self.token_file) as f:
            token = f.read().strip()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, context=self.context, timeout=30) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
        except OSError as e:
            raise Failure(f"Kubernetes API {method} {path}: {e}")

    def get_secret(self, namespace, name):
        status, body = self.request("GET", f"/api/v1/namespaces/{namespace}/secrets/{name}")
        if status == 404:
            raise Failure(f"Secret {namespace}/{name} does not exist; create it (secret.create) or pre-create it")
        if status != 200:
            raise Failure(f"reading Secret {namespace}/{name}: HTTP {status} {body.get('message', '')}")
        return body

    def replace_secret(self, secret):
        """PUT with the read resourceVersion: a concurrent change makes it fail (409)."""
        meta = secret["metadata"]
        status, body = self.request("PUT", f"/api/v1/namespaces/{meta['namespace']}/secrets/{meta['name']}", secret)
        if status == 409:
            raise Failure(f"Secret {meta['namespace']}/{meta['name']} changed concurrently; retrying in the next attempt")
        if status != 200:
            raise Failure(f"writing Secret {meta['namespace']}/{meta['name']}: HTTP {status} {body.get('message', '')}")
        return body

    def get_statefulset(self, namespace, name):
        status, body = self.request("GET", f"/apis/apps/v1/namespaces/{namespace}/statefulsets/{name}")
        if status != 200:
            raise Failure(f"reading StatefulSet {namespace}/{name}: HTTP {status} {body.get('message', '')}")
        return body

    def patch_statefulset(self, namespace, name, patch):
        status, body = self.request("PATCH", f"/apis/apps/v1/namespaces/{namespace}/statefulsets/{name}", patch, "application/merge-patch+json")
        if status != 200:
            raise Failure(f"patching StatefulSet {namespace}/{name}: HTTP {status} {body.get('message', '')}")


def rolled_out(statefulset):
    spec, status = statefulset.get("spec", {}), statefulset.get("status", {})
    replicas = spec.get("replicas", 1)
    return (status.get("observedGeneration", 0) >= statefulset["metadata"].get("generation", 0)
            and status.get("updatedReplicas", 0) == replicas and status.get("readyReplicas", 0) == replicas
            and status.get("currentRevision") == status.get("updateRevision"))


# --- Stalwart -----------------------------------------------------------------------------

class Stalwart:
    def __init__(self, url, user, password):
        self.url = url.rstrip("/") + "/jmap/"
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def call(self, method, args):
        body = json.dumps({"using": JMAP_USING, "methodCalls": [[method, args, "c"]]}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Authorization", self.auth)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                name, result, _ = json.load(resp)["methodResponses"][0]
        except (OSError, ValueError, KeyError) as e:
            raise Failure(f"Stalwart {method}: {e}")
        if name == "error":
            raise Failure(f"Stalwart {method}: {result.get('type')} {result.get('description', '')}")
        return result

    def account_id(self, name, address):
        ids = self.call("x:Account/query", {"filter": {"name": name}})["ids"]
        if ids:
            for account in self.call("x:Account/get", {"ids": ids, "properties": ["emailAddress"]})["list"]:
                if account.get("emailAddress", "").lower() == address.lower():
                    return account["id"]
        raise Failure(f"account {address} not found in Stalwart; the provisioning Job creates it")

    def app_passwords(self, account_id):
        return self.call("x:AppPassword/get", {"accountId": account_id})["list"]

    def create_app_password(self, account_id, value):
        result = self.call("x:AppPassword/set", {"accountId": account_id, "create": {"new": value}})
        if "new" not in result.get("created", {}):
            error = result.get("notCreated", {}).get("new", {})
            raise Failure(f"creating the app password: {error.get('type')} {error.get('description', '')} {error.get('properties', '')}")
        return result["created"]["new"]["id"], result["created"]["new"]["secret"]

    def update_app_password(self, account_id, stalwart_id, value):
        result = self.call("x:AppPassword/set", {"accountId": account_id, "update": {stalwart_id: value}})
        if stalwart_id not in result.get("updated", {}):
            error = result.get("notUpdated", {}).get(stalwart_id, {})
            raise Failure(f"updating the app password: {error.get('type')} {error.get('description', '')} {error.get('properties', '')}")

    def settings_marker(self, release):
        """(role id, state, stamp) of the settings marker, or (None, None, None)."""
        prefix = f"managed:{release}/settings "
        for role in self.call("x:Role/get", {"properties": ["description"]})["list"]:
            if (role.get("description") or "").startswith(prefix):
                state, _, stamp = role["description"][len(prefix):].partition(" ")
                return role["id"], state, stamp
        return None, None, None

    def set_settings_marker(self, release, role_id, state, stamp):
        """Creates or updates the marker; returns its role id."""
        value = {"description": f"managed:{release}/settings {state} {stamp}"}
        if role_id:
            result = self.call("x:Role/set", {"update": {role_id: value}})
            if role_id in result.get("updated", {}):
                return role_id
        else:
            result = self.call("x:Role/set", {"create": {"marker": value}})
            if "marker" in result.get("created", {}):
                return result["created"]["marker"]["id"]
        raise Failure(f"writing the settings marker: {result}")

    def ensure_admin_account(self, release, name, domain):
        """Creates the administrator account and its domain, or restores the Admin role."""
        ids = self.call("x:Domain/query", {"filter": {"name": domain}})["ids"]
        if ids:
            domain_id = ids[0]
        else:
            result = self.call("x:Domain/set", {"create": {"d": {"name": domain}}})
            if "d" not in result.get("created", {}):
                raise Failure(f"creating the domain {domain}: {result.get('notCreated')}")
            domain_id = result["created"]["d"]["id"]
        value = {"description": f"managed:{release}/admin", "roles": {"@type": "Admin"}}
        address = f"{name}@{domain}"
        for account in self._accounts(name):
            if account.get("emailAddress", "").lower() == address.lower():
                result = self.call("x:Account/set", {"update": {account["id"]: value}})
                if account["id"] not in result.get("updated", {}):
                    raise Failure(f"updating the account {address}: {result.get('notUpdated')}")
                return account["id"]
        result = self.call("x:Account/set", {"create": {"a": {"@type": "User", "name": name, "domainId": domain_id, **value}}})
        if "a" not in result.get("created", {}):
            raise Failure(f"creating the account {address}: {result.get('notCreated')}")
        log("admin", f"account {address} created")
        return result["created"]["a"]["id"]

    def _accounts(self, name):
        ids = self.call("x:Account/query", {"filter": {"name": name}})["ids"]
        return self.call("x:Account/get", {"ids": ids, "properties": ["emailAddress"]})["list"] if ids else []

    def destroy_app_passwords(self, account_id, ids):
        if not ids:
            return
        result = self.call("x:AppPassword/set", {"accountId": account_id, "destroy": list(ids)})
        failed = {k: v for k, v in result.get("notDestroyed", {}).items() if v.get("type") != "notFound"}
        if failed:
            raise Failure(f"revoking app passwords: {failed}")


def smtp_check(check, address, password, attempts=3, wait=3):
    """SMTP AUTH on the listener the consumers use. A failure fails the run, never rotates.
    Retried briefly: Stalwart may refuse a login for a moment after app passwords changed."""
    for attempt in range(1, attempts + 1):
        try:
            with smtplib.SMTP(check["host"], check["port"], timeout=20) as smtp:
                smtp.ehlo("stalwart-reconcile.invalid")
                if check.get("startTls"):
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo("stalwart-reconcile.invalid")
                smtp.login(address, password)
            return
        except (smtplib.SMTPException, OSError) as e:
            if attempt == attempts:
                raise Failure(f"SMTP AUTH as {address} at {check['host']}:{check['port']} failed: {e}")
            time.sleep(wait)


# --- Restart ------------------------------------------------------------------------------

def blob_key(config, stalwart):
    """Points Email and FileStorage at the chart's SymmetricKey. Returns True when it changed
    a setting."""
    wanted = config.get("blobKey")
    if not wanted:
        return False
    keys = stalwart.call("x:SymmetricKey/get", {}).get("list", [])
    key_id = next((k["id"] for k in keys if k.get("description") == wanted["description"]), None)
    if key_id is None:
        raise Failure(f"SymmetricKey \"{wanted['description']}\" not found; the provisioning Job creates it")
    changed = False
    for obj, enabled in (("Email", wanted["email"]), ("FileStorage", wanted["fileStorage"])):
        found = stalwart.call(f"x:{obj}/get", {"ids": ["singleton"], "properties": ["blobEncryptionKey"]}).get("list", [])
        current = found[0].get("blobEncryptionKey") if found else None
        # Disabled: clear only our own key, never one configured outside the chart.
        target = key_id if enabled else (None if current == key_id else current)
        if current == target:
            continue
        result = stalwart.call(f"x:{obj}/set", {"update": {"singleton": {"blobEncryptionKey": target}}})
        if "singleton" not in result.get("updated", {}):
            error = result.get("notUpdated", {}).get("singleton", {})
            raise Failure(f"{obj}.blobEncryptionKey: {error.get('type')} {error.get('description', '')}")
        log("blob-key", f"{obj}: " + (f"new blobs encrypted with key {key_id}" if target else "new blobs no longer encrypted"))
        changed = True
    return changed


def restart(config, kube, stalwart, force=False):
    """Rolls the StatefulSet when the pods may not run the current settings plan, or when force
    is set, waits until it is ready, then records the plan in Stalwart's database."""
    r, wanted = config["restart"], config["restart"]["settingsHash"]
    statefulset = kube.get_statefulset(r["namespace"], r["statefulset"])
    applied = statefulset["spec"]["template"]["metadata"].get("annotations", {}).get(SETTINGS, "")
    role_id, state, stamp = stalwart.settings_marker(config["release"])
    marked = (stamp or "").split("@")[0] == wanted
    if state == "done" and marked and applied.split("@")[0] == wanted and not force:
        log("restart", "settings unchanged, no restart")
        return
    if state == "pending" and marked:
        # An earlier run patched (or was about to patch) this stamp: resume, do not roll again.
        log("restart", f"resuming the rollout {stamp} of an earlier run")
    else:
        if force:
            reason = "blob encryption key setting changed"
        elif applied.split("@")[0] != wanted:
            reason = "settings changed"
        else:
            reason = "settings not yet loaded from Stalwart's database (reset?)"
        log("restart", f"{reason}; restarting StatefulSet {r['statefulset']}")
        # The time makes the pod template change even when only the marker was missing.
        stamp = f"{wanted}@{int(time.time())}"
        role_id = stalwart.set_settings_marker(config["release"], role_id, "pending", stamp)
    if applied != stamp:
        kube.patch_statefulset(r["namespace"], r["statefulset"], {"spec": {"template": {"metadata": {"annotations": {SETTINGS: stamp}}}}})
    deadline = time.time() + r["timeoutSeconds"]
    while not rolled_out(kube.get_statefulset(r["namespace"], r["statefulset"])):
        if time.time() > deadline:
            raise Failure(f"StatefulSet {r['statefulset']} did not become ready within {r['timeoutSeconds']}s")
        time.sleep(5)
    stalwart.set_settings_marker(config["release"], role_id, "done", stamp)
    log("restart", "settings applied")


# --- System accounts ----------------------------------------------------------------------

def normalized_networks(networks):
    """Networks as Stalwart stores them: a single address without its prefix length."""
    result = set()
    for network in networks:
        net = ipaddress.ip_network(network, strict=False)
        result.add(str(net.network_address) if net.prefixlen == net.max_prefixlen else str(net))
    return result


def reconcile_account(config, account, kube, stalwart):
    name, address = account["id"], account["address"]
    marker = f"managed:{config['release']}/{account['id']}"
    ns, secret_name = account["secret"]["namespace"], account["secret"]["name"]
    # The lock belongs to the Job, so its retried pods continue where a failed one stopped.
    owner = os.environ.get("JOB_UID") or os.environ.get("HOSTNAME", "job")
    holder = f"{owner}@{int(time.time())}"

    secret = kube.get_secret(ns, secret_name)
    annotations = secret["metadata"].setdefault("annotations", {})
    held = annotations.get(LOCK, "")
    if held:
        held_by, _, held_since = held.rpartition("@")
        if held_by != owner and int(held_since) > time.time() - config["lockTimeoutSeconds"]:
            raise Failure(f"Secret {ns}/{secret_name} is locked by another run ({held})")
    annotations[LOCK] = holder
    secret = kube.replace_secret(secret)

    try:
        account_id = stalwart.account_id(account["name"], address)
        current = secret["metadata"]["annotations"].get(CREDENTIAL, "")
        # Managed app passwords: reference -> app password. One without a reference is an orphan.
        managed, orphans = {}, []
        for p in stalwart.app_passwords(account_id):
            description = p.get("description") or ""
            if description == marker:
                orphans.append(p["id"])
            elif description.startswith(marker + " #"):
                managed[description[len(marker) + 2:]] = p
        # Credentials of a run that died before recording them can never be used: revoke them.
        orphans += [p["id"] for ref, p in managed.items() if ref != current]
        if orphans:
            log(name, f"revoking {len(orphans)} unrecorded credential(s) of interrupted runs")
            stalwart.destroy_app_passwords(account_id, orphans)

        data = secret.get("data") or {}
        keys = account["secret"]["keys"]
        stored = next((data[k] for k in keys if data.get(k)), None)
        if current in managed and not stored:
            # The Secret lost the password; Stalwart cannot show it again.
            log(name, f"Secret {ns}/{secret_name} holds no password for credential {current}; replacing it")
            stalwart.destroy_app_passwords(account_id, [managed.pop(current)["id"]])
        if current and current in managed:
            # Restrictions follow the values: changed networks or permissions are applied to the
            # existing app password, which keeps its secret.
            existing = managed[current]
            wanted = account["appPassword"]
            if (normalized_networks(existing.get("allowedIps") or {}) != normalized_networks(wanted["allowedIps"])
                    or existing.get("permissions") != wanted["permissions"]):
                stalwart.update_app_password(account_id, existing["id"], {
                    "allowedIps": {ip: True for ip in wanted["allowedIps"]}, "permissions": wanted["permissions"]})
                log(name, f"restrictions of credential {current} updated")
            password = base64.b64decode(stored).decode()
            encoded = stored
            if any(data.get(k) != encoded for k in keys):
                secret["data"] = {**data, **{k: encoded for k in keys}}
                secret = kube.replace_secret(secret)
                log(name, f"keys {', '.join(keys)} updated with the recorded credential {current}")
            if config.get("smtpCheck"):
                smtp_check(config["smtpCheck"], address, password)
            log(name, f"credential {current} in place")
            return

        log(name, "creating an app password" if not current else f"credential {current} no longer exists in Stalwart; creating a new one")
        reference = secrets.token_hex(8)
        value = {"description": f"{marker} #{reference}", "allowedIps": {ip: True for ip in account["appPassword"]["allowedIps"]},
                 "permissions": account["appPassword"]["permissions"]}
        new_id, password = stalwart.create_app_password(account_id, value)
        if config.get("smtpCheck"):
            try:
                smtp_check(config["smtpCheck"], address, password)
            except Failure:
                stalwart.destroy_app_passwords(account_id, [new_id])
                raise
        encoded = base64.b64encode(password.encode()).decode()
        secret["data"] = {**data, **{k: encoded for k in keys}}
        secret["metadata"]["annotations"][CREDENTIAL] = reference
        secret = kube.replace_secret(secret)
        log(name, f"published credential {reference} to Secret {ns}/{secret_name}; applications pick it up when they restart")
    finally:
        try:
            latest = kube.get_secret(ns, secret_name)
            if latest["metadata"].get("annotations", {}).get(LOCK) == holder:
                latest["metadata"]["annotations"].pop(LOCK)
                kube.replace_secret(latest)
        except Failure as e:
            log(name, f"could not release the lock (it expires after {config['lockTimeoutSeconds']}s): {e}")


# --- Administrator account ----------------------------------------------------------------

def login_accepted(url, user, password, attempts=5, wait=3):
    """True when Stalwart accepts the login, False when it refuses it. Connection errors are
    retried: right after a restart the Service may not route to the new pods yet."""
    req = urllib.request.Request(url.rstrip("/") + "/jmap/session")
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode())
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=20):
                return True
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return False
            error = f"HTTP {e.code}"
        except OSError as e:
            error = str(e)
        if attempt < attempts:
            time.sleep(wait)
    raise Failure(f"Stalwart at {url}: {error}")


def roll(config, kube, stamp):
    """Restarts Stalwart, so it reads STALWART_RECOVERY_ADMIN again, and waits until it is ready.
    The stamp stays on the pod template: "recovery@..." while the pods may run with the recovery
    administrator, "done@..." once a rollout without it was requested."""
    r = config["restart"]
    kube.patch_statefulset(r["namespace"], r["statefulset"], {"spec": {"template": {"metadata": {"annotations": {BOOTSTRAP: stamp}}}}})
    wait_rollout(config, kube)


def wait_rollout(config, kube):
    r = config["restart"]
    deadline = time.time() + r["timeoutSeconds"]
    while not rolled_out(kube.get_statefulset(r["namespace"], r["statefulset"])):
        if time.time() > deadline:
            raise Failure(f"StatefulSet {r['statefulset']} did not become ready within {r['timeoutSeconds']}s")
        time.sleep(5)


def admin_credential(admin):
    """The Jobs' app password as a system account entry, for reconcile_account."""
    return {"id": "admin", "name": admin["name"], "address": admin["address"],
            "secret": {**admin["secret"], "keys": ["password"]},
            "appPassword": {"allowedIps": admin["allowedIps"], "permissions": {"@type": "Inherit"}}}


def decoded(data, key):
    return base64.b64decode(data[key]).decode() if data.get(key) else ""


def bootstrap(config, kube):
    admin = config["adminAccount"]
    url, ns, name = config["stalwartUrl"], admin["secret"]["namespace"], admin["secret"]["name"]
    data = kube.get_secret(ns, name).get("data") or {}
    password = decoded(data, "password")
    if password and login_accepted(url, admin["address"], password):
        r = config["restart"]
        statefulset = kube.get_statefulset(r["namespace"], r["statefulset"])
        stamp = statefulset["spec"]["template"]["metadata"].get("annotations", {}).get(BOOTSTRAP, "")
        if data.get(RECOVERY_KEY) or stamp.startswith("recovery@"):
            # An earlier run stopped before the pods restarted without the recovery
            # administrator (or one was added for an emergency): the pods may still have it.
            remove_recovery_admin(config, kube)
        elif not rolled_out(statefulset):
            # The rollout without it was requested, but may not have finished.
            log("admin", "waiting for the rollout without the recovery administrator")
            wait_rollout(config, kube)
        log("admin", f"account {admin['address']} in place")
        return

    log("admin", "no working password recorded (first run, database reset or revoked); bootstrapping"
        if not password else "the recorded password no longer works; bootstrapping")
    recovery = decoded(data, RECOVERY_KEY)
    if not recovery:
        # Random and only in this Secret: used by this Job, removed again below.
        recovery = f"bootstrap-{secrets.token_hex(4)}:{secrets.token_urlsafe(32)}"
        secret = kube.get_secret(ns, name)
        secret["data"] = {**(secret.get("data") or {}), RECOVERY_KEY: base64.b64encode(recovery.encode()).decode()}
        kube.replace_secret(secret)
    user, _, recovery_password = recovery.partition(":")
    if not login_accepted(url, user, recovery_password):
        log("admin", "restarting Stalwart with a temporary recovery administrator")
        roll(config, kube, f"recovery@{int(time.time())}")
        if not login_accepted(url, user, recovery_password):
            raise Failure("Stalwart does not accept the temporary recovery administrator after the restart")

    stalwart = Stalwart(url, user, recovery_password)
    stalwart.ensure_admin_account(config["release"], admin["name"], admin["domain"])
    if password:
        # A password Stalwart refuses but still knows (e.g. restricted otherwise): replace it.
        secret = kube.get_secret(ns, name)
        secret["data"] = {k: v for k, v in (secret.get("data") or {}).items() if k != "password"}
        kube.replace_secret(secret)
    reconcile_account({**config, "smtpCheck": None}, admin_credential(admin), kube, stalwart)
    password = decoded(kube.get_secret(ns, name).get("data") or {}, "password")
    if not login_accepted(url, admin["address"], password):
        raise Failure(f"Stalwart refuses the new app password of {admin['address']}")
    remove_recovery_admin(config, kube)
    if login_accepted(url, user, recovery_password):
        raise Failure("Stalwart still accepts the temporary recovery administrator after the restart")
    log("admin", f"account {admin['address']} in place; recovery administrator removed")


def remove_recovery_admin(config, kube):
    admin = config["adminAccount"]
    secret = kube.get_secret(admin["secret"]["namespace"], admin["secret"]["name"])
    secret["data"] = {k: v for k, v in (secret.get("data") or {}).items() if k != RECOVERY_KEY}
    kube.replace_secret(secret)
    log("admin", "restarting Stalwart without the temporary recovery administrator")
    roll(config, kube, f"done@{int(time.time())}")


def main():
    with open(os.environ.get("CONFIG_FILE", "/config/config.json")) as f:
        config = json.load(f)
    kube = Kubernetes()
    if sys.argv[1:] == ["bootstrap"]:
        try:
            bootstrap(config, kube)
        except Failure as e:
            log("admin", f"FAILED: {e}")
            sys.exit(1)
        sys.exit(0)
    stalwart = Stalwart(config["stalwartUrl"], os.environ["STALWART_USER"], os.environ["STALWART_PASSWORD"])
    try:
        force = blob_key(config, stalwart)
        if config.get("restart"):
            restart(config, kube, stalwart, force)
    except Failure as e:
        log("restart", f"FAILED: {e}")
        sys.exit(1)
    failed = False
    # App passwords of the administrator account for other clients: no SMTP check, they do not send.
    clients = (config.get("adminAccount") or {}).get("clients", [])
    for account, account_config in [(a, config) for a in config["accounts"]] + [(c, {**config, "smtpCheck": None}) for c in clients]:
        try:
            reconcile_account(account_config, account, kube, stalwart)
        except Failure as e:
            log(account["id"], f"FAILED: {e}")
            failed = True
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
