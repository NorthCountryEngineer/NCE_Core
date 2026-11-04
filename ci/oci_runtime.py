"""Exercise built application images using isolated, disposable synthetic state.

No host mounts, published ports, customer services, or build credentials enter
these containers. The internal Docker network has no route to external services.
"""
import json
import re
import secrets
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

    try:
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
        command(["docker", "exec", names["browser"], "python", "-c", CHROMIUM_PROBE])
        start("api", images["api"], user="10001:10001", mounts=api_mounts, variables=runtime_variables)
        probe("http://127.0.0.1:8000/api/health")
        probe("http://" + names["browser"] + ":8010/health")
        start("web", images["web"], user="101:101", mounts=[
            ("/var/cache/nginx", "rw,nosuid,nodev,size=32m,uid=101,gid=101,mode=0700"),
            ("/var/run", "rw,nosuid,nodev,size=8m,uid=101,gid=101,mode=0700"),
            ("/etc/nginx/conf.d", "rw,nosuid,nodev,size=8m,uid=101,gid=101,mode=0700")],
              variables=["STUDIO_API_UPSTREAM=" + names["api"], "NGINX_ENVSUBST_FILTER=STUDIO_API_UPSTREAM"])
        probe("http://" + names["web"] + ":8080/health", "text")
        probe("http://" + names["web"] + ":8080/api/health")
        return {"status": "passed", "database_fixture": fixture, "synthetic_database_migration": "passed",
                "non_root_read_only": "passed", "frontend_api_proxy": "passed", "chromium_launch": "passed",
                "browser_health": "passed", "external_network": "isolated", "customer_services_accessed": False}
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
