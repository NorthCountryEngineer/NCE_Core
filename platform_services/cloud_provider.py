"""Choose only operator-bound Lightsail hosting; preserve other AWS recipes."""
from . import cloud_aws, cloud_lightsail


def hosting_fingerprint(app):
    return cloud_lightsail.hosting_fingerprint(app)


def _adapter(platform, snapshot):
    return cloud_lightsail if cloud_lightsail.binding_for(platform, snapshot) else cloud_aws


def start_run(platform, snapshot, key):
    return _adapter(platform, snapshot).start_run(platform, snapshot, key)


def recover_run(platform, snapshot, key):
    return _adapter(platform, snapshot).recover_run(platform, snapshot, key)


def poll_run(platform, snapshot, state):
    return _adapter(platform, snapshot).poll_run(platform, snapshot, state)


def stop_run(platform, snapshot, state):
    return _adapter(platform, snapshot).stop_run(platform, snapshot, state)


def store_credentials(platform, name, value, *, snapshot):
    adapter = _adapter(platform, snapshot)
    if adapter is cloud_lightsail:
        return adapter.store_credentials(platform, name, value, snapshot=snapshot)
    return adapter.store_credentials(platform, name, value)
