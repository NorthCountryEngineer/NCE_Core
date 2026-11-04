"""Exercise built application images using isolated, disposable synthetic state.

No host mounts, published ports, customer services, or build credentials enter
these containers. The internal Docker network has no route to external services.
"""
import json
from pathlib import Path, PurePosixPath
import re
import secrets
import tarfile
import tempfile
import uuid


DATABASE_FIXTURE = "postgres:16-alpine"
HTTP_PROBE = """import json,sys,time,urllib.request
deadline = time.monotonic() + 120
while True:
    try:
        with urllib.request.urlopen(sys.argv[1], timeout=3) as response:
            assert response.status == 200
            body = response.read(1024 * 1024)
            if sys.argv[2] == 'json':
                assert json.loads(body)['status'] == 'ok'
            else:
                assert body.strip() == b'ok'
        break
    except Exception:
        if time.monotonic() >= deadline:
            raise SystemExit('Synthetic runtime health check did not become ready')
        time.sleep(1)
"""
CHROMIUM_PROBE = """import asyncio,os
from playwright.async_api import async_playwright
async def main():
    assert os.getuid() == 10002
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True,
            proxy={'server': 'http://127.0.0.1:9'},
            args=['--force-webrtc-ip-handling-policy=disable_non_proxied_udp'])
        page = await browser.new_page()
        await page.set_content('<title>NCE synthetic runtime check</title>')
        assert await page.title() == 'NCE synthetic runtime check'
        await browser.close()
asyncio.run(main())
"""
SOURCE_PROBE = """import hashlib,importlib.util,json,pathlib,sys
def reject():
    raise SystemExit('Compiled runtime content validation failed')
component = sys.argv[1]
root = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else pathlib.Path('/app')
required = {
    'api': {'studio/__init__.pyc', 'studio/main.pyc', 'studio/migrate.pyc',
            'studio/storage_setup.pyc', 'scripts/__init__.pyc',
            'scripts/migrate_reference_storage.pyc',
            'integrations/opportunity-readback/bridge.pyc'},
    'browser': {'studio/__init__.pyc', 'studio/sam_browser_transport.pyc',
                'studio/sam_browser_worker.pyc', 'studio/browser_service.pyc'},
}
if component not in required or not root.is_dir() or root.is_symlink():
    reject()
manifest_path = root / 'runtime-package.json'
if not manifest_path.is_file() or manifest_path.is_symlink() or manifest_path.stat().st_size > 1024 * 1024:
    reject()
try:
    manifest = json.loads(manifest_path.read_text())
except (OSError, UnicodeError, ValueError):
    reject()
if (not isinstance(manifest, dict)
        or set(manifest) != {'schema_version', 'component', 'python', 'code', 'assets'}
        or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1
        or manifest['component'] != component or manifest['python'] != sys.implementation.cache_tag
        or not isinstance(manifest['code'], dict) or not isinstance(manifest['assets'], dict)
        or not required[component] <= set(manifest['code'])):
    reject()
actual_code = set()
for path in root.rglob('*'):
    relative = path.relative_to(root)
    if (path.is_symlink() or any(part.lower() in {'.git', 'test', 'tests', '__tests__', '__pycache__', 'node_modules'} for part in relative.parts)
            or path.suffix.lower() in {'.py', '.zip', '.tar', '.gz', '.tgz', '.pyz'}
            or path.suffix.lower() == '.pyc' and path.name.lower().startswith('test_')):
        reject()
    if path.is_file() and path.suffix.lower() == '.pyc':
        actual_code.add(relative.as_posix())
if actual_code != set(manifest['code']):
    reject()
for kind in ('code', 'assets'):
    for name, expected in manifest[kind].items():
        if not isinstance(name, str) or not isinstance(expected, str):
            reject()
        relative = pathlib.PurePosixPath(name)
        if relative.is_absolute() or relative.as_posix() != name or any(part in {'', '.', '..'} for part in relative.parts):
            reject()
        path = root / name
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
            reject()
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            reject()
        if kind == 'code' and (path.suffix != '.pyc' or len(data) <= 16 or data[:4] != importlib.util.MAGIC_NUMBER):
            reject()
        if kind == 'assets' and path.suffix.lower() in {'.py', '.pyc'}:
            reject()
if component == 'api' and not {'CORE_CONTRACT.json', 'integrations/opportunity-readback/schema.json'} <= set(manifest['assets']):
    reject()
print(json.dumps({'source_packaging': 'cpython-bytecode', 'original_source_exclusion': 'passed',
                  'compiled_modules': len(actual_code)}))
"""
WEB_SOURCE_PROBE = """set -eu
root="${1:-/usr/share/nginx/html}"
test -d "$root" && test ! -L "$root" && test -f "$root/index.html"
found="$(find "$root" \\( -type l -o \\( -type d \\( -iname .git -o -iname test -o -iname tests -o -iname __tests__ -o -iname __pycache__ -o -iname test-results -o -iname playwright-report -o -iname node_modules \\) \\) -o \\( -type f \\( -iname '*.ts' -o -iname '*.tsx' -o -iname '*.jsx' -o -iname '*.map' -o -iname '*.py' -o -iname '*.zip' -o -iname '*.tar' -o -iname '*.gz' -o -iname '*.tgz' -o -iname '*.pyz' \\) \\) \\) -print -quit)"
test -z "$found"
pattern='(^|[;}])[[:space:]]*(//|/\\*)[#@][[:space:]]*sourceMappingURL[[:space:]]*=[[:space:]]*data:'
found="$(find "$root" -type f \\( -iname '*.js' -o -iname '*.css' \\) -exec sh -c 'grep -E -l "$1" "$2"; probe_status=$?; if [ "$probe_status" -gt 1 ]; then printf "%s\\n" "unreadable frontend asset"; fi' probe "$pattern" {} \\;)"
test -z "$found"
printf '%s\\n' '{"source_packaging":"javascript-bundle","original_source_exclusion":"passed"}'
"""


def _layer_source_path(name):
    """Classify application paths only; separately licensed vendor code remains."""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError("Unsafe image layer path")
    parts = tuple(part.lower() for part in path.parts)
    if parts[:1] == ("app",):
        return (any(part in {".git", "test", "tests", "__tests__", "__pycache__", "node_modules"} for part in parts)
                or path.suffix.lower() in {".py", ".zip", ".tar", ".gz", ".tgz", ".pyz"}
                or path.suffix.lower() == ".pyc" and path.name.lower().startswith("test_"))
    if parts[:4] == ("usr", "share", "nginx", "html"):
        return (any(part in {".git", "test", "tests", "__tests__", "__pycache__", "test-results", "playwright-report", "node_modules"} for part in parts[4:])
                or path.suffix.lower() in {".ts", ".tsx", ".jsx", ".map", ".py", ".zip", ".tar", ".gz", ".tgz", ".pyz"})
    return False


def image_layer_source_exclusion(images, *, execute, env):
    """Inspect every final-image layer, including files hidden by whiteouts."""
    from ci.oci_release import ReleaseError

    counts = {}
    try:
        with tempfile.TemporaryDirectory(prefix="nce-image-layer-audit-") as folder:
            for component, reference in sorted(images.items()):
                archive_path = Path(folder) / (component + ".tar")
                execute(["docker", "image", "save", "--output", str(archive_path), reference], env=env)
                with tarfile.open(archive_path, mode="r:*") as archive:
                    entries = archive.getmembers()
                    names = [entry.name for entry in entries]
                    if len(entries) > 4096 or len(names) != len(set(names)):
                        raise ValueError("Ambiguous image archive")
                    manifest_entry = archive.getmember("manifest.json")
                    if not manifest_entry.isfile() or manifest_entry.size > 1024 * 1024:
                        raise ValueError("Invalid image archive manifest")
                    with archive.extractfile(manifest_entry) as stream:
                        manifests = json.loads(stream.read(1024 * 1024 + 1))
                    if (not isinstance(manifests, list) or len(manifests) != 1
                            or not isinstance(manifests[0], dict)
                            or manifests[0].get("RepoTags") != [reference]):
                        raise ValueError("Image archive does not match the built image")
                    manifest = manifests[0]
                    layers = manifest.get("Layers")
                    if (not isinstance(layers, list) or not 1 <= len(layers) <= 256
                            or any(not isinstance(name, str) for name in layers)
                            or len(layers) != len(set(layers))):
                        raise ValueError("Invalid image archive layers")
                    for name in [manifest.get("Config"), *layers]:
                        if not isinstance(name, str):
                            raise ValueError("Invalid image archive metadata path")
                        path = PurePosixPath(name)
                        if path.is_absolute() or path.as_posix() != name or any(part in {"", ".", ".."} for part in path.parts) or "\\" in name:
                            raise ValueError("Unsafe image archive metadata path")
                        if not archive.getmember(name).isfile():
                            raise ValueError("Image archive metadata must be regular files")
                    total = 0
                    for name in layers:
                        with archive.extractfile(name) as stream, tarfile.open(fileobj=stream, mode="r|*") as layer:
                            for entry in layer:
                                total += 1
                                if total > 1_000_000 or _layer_source_path(entry.name):
                                    raise ValueError("Original application source appears in a final image layer")
                                parts = PurePosixPath(entry.name).parts
                                application = parts[:1] == ("app",) or parts[:4] == ("usr", "share", "nginx", "html")
                                if application and (entry.issym() or entry.islnk()):
                                    raise ValueError("Application runtime links are not allowed")
                    counts[component] = len(layers)
                archive_path.unlink()
    except (OSError, tarfile.TarError, ValueError, TypeError, KeyError):
        raise ReleaseError("Final image layer source exclusion failed.") from None
    return counts


def runtime_smoke(images, *, execute, env):
    """Return bounded evidence only after migration, HTTP and Chromium checks."""
    from ci.oci_release import ReleaseError

    if set(images) != {"web", "api", "browser"}:
        raise ReleaseError("Runtime verification requires all three built application images.")
    prefix = "nce-oci-smoke-" + uuid.uuid4().hex[:12]
    names = {component: prefix + "-" + component for component in ("db", "migrate", "api", "browser", "web")}
    network_created = False
    attempted = []
    failed = False

    def command(args):
        return execute(args, env=env)

    def start(component, image, *, user, mounts, variables=(), arguments=(), detached=True):
        attempted.append(names[component])
        args = ["docker", "run", "--name", names[component], "--network", prefix,
                "--read-only", "--user", user, "--cap-drop", "ALL", "--security-opt",
                "no-new-privileges:true", "--pids-limit", "256", "--memory", "1g", "--cpus", "1"]
        args.append("--detach" if detached else "--rm")
        for path, options in mounts:
            args.extend(["--tmpfs", path + ":" + options])
        for variable in variables:
            args.extend(["--env", variable])
        args.extend([image, *arguments])
        command(args)

    def probe(url, kind="json"):
        command(["docker", "exec", names["api"], "python", "-c", HTTP_PROBE, url, kind])

    def source_probe(component):
        try:
            evidence = json.loads(command(["docker", "exec", names[component], "python", "-c", SOURCE_PROBE, component]))
        except (ValueError, TypeError):
            raise ReleaseError("Compiled runtime source exclusion returned invalid evidence.") from None
        if (not isinstance(evidence, dict) or evidence.get("source_packaging") != "cpython-bytecode"
                or evidence.get("original_source_exclusion") != "passed"
                or type(evidence.get("compiled_modules")) is not int or evidence["compiled_modules"] < 1):
            raise ReleaseError("Compiled runtime source exclusion has not passed.")
        return evidence

    def web_source_probe():
        try:
            evidence = json.loads(command(["docker", "exec", names["web"], "sh", "-c", WEB_SOURCE_PROBE]))
        except (ValueError, TypeError):
            raise ReleaseError("Frontend source exclusion returned invalid evidence.") from None
        if evidence != {"source_packaging": "javascript-bundle", "original_source_exclusion": "passed"}:
            raise ReleaseError("Frontend original source exclusion has not passed.")

    try:
        layer_counts = image_layer_source_exclusion(images, execute=execute, env=env)
        command(["docker", "pull", DATABASE_FIXTURE])
        try:
            fixture_refs = json.loads(command(["docker", "image", "inspect", DATABASE_FIXTURE,
                                              "--format", "{{json .RepoDigests}}"]))
        except (ValueError, TypeError):
            raise ReleaseError("The disposable PostgreSQL fixture has no immutable identity.") from None
        if not isinstance(fixture_refs, list):
            raise ReleaseError("The disposable PostgreSQL fixture has no immutable identity.")
        fixture = next((reference for reference in fixture_refs
                        if isinstance(reference, str) and re.fullmatch(r"postgres@sha256:[a-f0-9]{64}", reference)), "")
        if not fixture:
            raise ReleaseError("The disposable PostgreSQL fixture has no immutable identity.")
        command(["docker", "network", "create", "--internal", prefix])
        network_created = True
        start("db", fixture, user="70:70", mounts=[
            ("/var/lib/postgresql/data", "rw,nosuid,nodev,size=256m,uid=70,gid=70,mode=0700"),
            ("/var/run/postgresql", "rw,nosuid,nodev,size=8m,uid=70,gid=70,mode=0700"),
            ("/tmp", "rw,nosuid,nodev,size=32m,mode=1777")], variables=[
                "POSTGRES_USER=studio", "POSTGRES_DB=release_smoke", "POSTGRES_HOST_AUTH_METHOD=trust"])
        command(["docker", "exec", names["db"], "sh", "-c",
                 "for i in $(seq 1 60); do pg_isready -U studio -d release_smoke >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1"])
        runtime_variables = [
            "DATABASE_URL=postgresql+psycopg://studio@" + names["db"] + ":5432/release_smoke",
            "SESSION_SECRET=" + secrets.token_hex(32), "AUTH_MODE=dev", "APP_URL=http://localhost:8080",
            "STORAGE_BACKEND=local", "LOCAL_STORAGE_PATH=/app/.local/blobs", "AWS_EC2_METADATA_DISABLED=true",
            "SAM_BROWSER_URL=http://" + names["browser"] + ":8010", "WEB_CONCURRENCY=1"]
        api_mounts = [("/tmp", "rw,nosuid,nodev,size=256m,uid=10001,gid=10001,mode=0700"),
                      ("/app/.local", "rw,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700")]
        start("migrate", images["api"], user="10001:10001", mounts=api_mounts,
              variables=runtime_variables, arguments=["python", "-m", "studio.migrate"], detached=False)
        start("browser", images["browser"], user="10002:10002", mounts=[
            ("/tmp", "rw,nosuid,nodev,size=1g,uid=10002,gid=10002,mode=0700"),
            ("/dev/shm", "rw,nosuid,nodev,size=256m,uid=10002,gid=10002,mode=0700")])
        browser_package = source_probe("browser")
        command(["docker", "exec", names["browser"], "python", "-c", CHROMIUM_PROBE])
        start("api", images["api"], user="10001:10001", mounts=api_mounts, variables=runtime_variables)
        api_package = source_probe("api")
        command(["docker", "exec", names["api"], "python", "-m", "scripts.migrate_reference_storage", "--help"])
        probe("http://127.0.0.1:8000/api/health")
        probe("http://" + names["browser"] + ":8010/health")
        start("web", images["web"], user="101:101", mounts=[
            ("/var/cache/nginx", "rw,nosuid,nodev,size=32m,uid=101,gid=101,mode=0700"),
            ("/var/run", "rw,nosuid,nodev,size=8m,uid=101,gid=101,mode=0700"),
            ("/etc/nginx/conf.d", "rw,nosuid,nodev,size=8m,uid=101,gid=101,mode=0700")],
              variables=["STUDIO_API_UPSTREAM=" + names["api"], "NGINX_ENVSUBST_FILTER=STUDIO_API_UPSTREAM"])
        web_source_probe()
        probe("http://" + names["web"] + ":8080/health", "text")
        probe("http://" + names["web"] + ":8080/api/health")
        return {"status": "passed", "database_fixture": fixture, "synthetic_database_migration": "passed",
                "non_root_read_only": "passed", "frontend_api_proxy": "passed", "chromium_launch": "passed",
                "browser_health": "passed", "external_network": "isolated", "customer_services_accessed": False,
                "source_packaging": "cpython-bytecode", "original_source_exclusion": "passed",
                "frontend_packaging": "javascript-bundle",
                "layer_source_exclusion": "passed", "layer_counts": layer_counts,
                "compiled_modules": {"api": api_package["compiled_modules"], "browser": browser_package["compiled_modules"]},
                "compiled_storage_utility": "passed"}
    except Exception:
        failed = True
        raise
    finally:
        cleanup_failed = False
        for name in reversed(attempted):
            try:
                command(["docker", "rm", "--force", name])
            except Exception:
                # The one-shot migration container is removed automatically.
                if name != names["migrate"]:
                    cleanup_failed = True
        if network_created:
            try:
                command(["docker", "network", "rm", prefix])
            except Exception:
                cleanup_failed = True
        if cleanup_failed and not failed:
            raise ReleaseError("Disposable runtime verification resources could not be cleaned up.")
