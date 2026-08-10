"""One-off: the job check must install the runtime bootstrap in the test process.

Without bootstrap.install(), load_config() falls back to PROJECT_ROOT/config.yaml
(the repo one) instead of the temp workspace, so clean_root resolved to the real
dataset path which does not exist on this machine.

ASCII-only so no encoding conversion is needed.
"""
import pathlib

p = pathlib.Path(__file__).with_name("check_jobs.py")
with p.open("r", encoding="utf-8", newline="") as f:
    s = f.read()

pairs = [
    (
        "    from app.runtime import paths\r\n",
        "    from app.runtime import bootstrap, paths\r\n",
    ),
    (
        "            paths.set_workspace_override(workspace)\r\n"
        "            os.environ[paths.WORKSPACE_ENV] = str(workspace)\r\n",

        "            os.environ[paths.WORKSPACE_ENV] = str(workspace)\r\n"
        "            bootstrap.install(workspace=workspace)\r\n",
    ),
]

for old, new in pairs:
    if s.count(old) != 1:
        raise SystemExit("pattern not found exactly once (%d): %r"
                         % (s.count(old), old[:60]))
    s = s.replace(old, new, 1)

with p.open("w", encoding="utf-8", newline="") as f:
    f.write(s)
print("patched check_jobs.py")
