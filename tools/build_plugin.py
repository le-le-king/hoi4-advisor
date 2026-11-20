"""把插件目录打成一个 .neko-plugin 安装包。

格式（跟宿主/官方包一致，反推确认过）：

    manifest.toml            包元数据（id / 名称 / 版本 / 描述）
    metadata.toml            payload 的 sha256
    payload/dependencies.toml
    payload/plugins/<id>/…   插件自己的文件

payload hash 算法：`payload/` 下所有文件按相对路径排序，依次
`update(路径)` + `\\0` + `update(内容)` + `\\0`。少算一个文件宿主就报
「安装包内容与校验信息不一致」。

用法：
    python tools/build_plugin.py                      # 输出到 dist/
    python tools/build_plugin.py -o out/x.neko-plugin
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import sys
import tomllib
import zipfile
from pathlib import Path

# 不打包的东西：运行期数据、缓存、备份、第三方二进制
SKIP_DIRS = {
    "__pycache__", ".git", ".github", "vendor", "data", "cache", "dist",
    "build", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".vscode", ".idea",
    # 第三方语音识别运行时（随 Release 包分发，不进源码仓库）
    "asrlib",
}
SKIP_DIR_PREFIXES = ("_backup", "_new", ".")
SKIP_FILES = {"config.json", "config.toml"}
SKIP_SUFFIX = (".pyc", ".pyo", ".log", ".tmp", ".bak", ".pdb", ".lib", ".exp", ".obj")
SKIP_GLOBS = ("neko_hoi4_agent_r*.dll",)


def payload_hash(entries: list[tuple[str, bytes]]) -> str:
    h = hashlib.sha256()
    for rel, data in entries:
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(data)
        h.update(b"\x00")
    return h.hexdigest()


def collect(src: Path) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(
            d for d in dirs
            if d not in SKIP_DIRS and not d.startswith(SKIP_DIR_PREFIXES)
        )
        for name in sorted(files):
            if name in SKIP_FILES or name.endswith(SKIP_SUFFIX):
                continue
            if any(fnmatch.fnmatch(name, g) for g in SKIP_GLOBS):
                continue
            full = Path(root) / name
            rel = full.relative_to(src).as_posix()
            try:
                out.append((rel, full.read_bytes()))
            except OSError as exc:
                print(f"  ! 读不了 {rel}: {exc}", file=sys.stderr)
    out.sort(key=lambda x: x[0])
    return out


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="")
    args = ap.parse_args()

    toml_path = root / "plugin.toml"
    with toml_path.open("rb") as f:
        meta = tomllib.load(f)["plugin"]
    pid = str(meta["id"])
    name = str(meta.get("name") or pid)
    version = str(meta.get("version") or "0.0.0")
    desc = str(meta.get("description") or "")
    # manifest 是单行字符串，换行会把 toml 弄坏
    desc = desc.replace("\r", " ").replace("\n", " ").replace('"', "'")

    files = collect(root)
    if not files:
        print("没收集到任何文件", file=sys.stderr)
        return 1

    dependencies = (
        'schema_version = "1.0"\n'
        "\n"
        f"[plugins.{pid}]\n"
        "python_requirements = []\n"
        "host_python_requirements = []\n"
        "plugin_dependencies = []\n"
        "advanced_plugin_dependencies = []\n"
        f'vendor_path = "plugins/{pid}/vendor"\n'
        "vendor_present = false\n"
    )

    entries = [(f"plugins/{pid}/{rel}", data) for rel, data in files]
    entries.append(("dependencies.toml", dependencies.encode("utf-8")))
    entries.sort(key=lambda x: x[0])
    phash = payload_hash(entries)

    manifest = (
        'schema_version = "1.0"\n'
        'package_type = "plugin"\n'
        "\n"
        f'id = "{pid}"\n'
        f'package_name = "{name}"\n'
        f'version = "{version}"\n'
        f'package_description = "{desc}"\n'
    )
    meta_toml = (
        "[payload]\n"
        'hash_algorithm = "sha256"\n'
        f'hash = "{phash}"\n'
        "\n"
        "[source]\n"
        'kind = "local"\n'
        f'paths = ["{pid}"]\n'
    )

    out = Path(args.out) if args.out else root / "dist" / f"{pid}.neko-plugin"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.toml", manifest)
        z.writestr("metadata.toml", meta_toml)
        z.writestr("payload/dependencies.toml", dependencies.encode("utf-8"))
        for rel, data in entries:
            if rel == "dependencies.toml":
                continue
            z.writestr(f"payload/{rel}", data)

    size = out.stat().st_size / 1024 / 1024
    print(f"打包完成：{out}")
    print(f"  {pid} {version}（{name}）")
    print(f"  payload 文件 {len(entries)} 个 / {size:.1f} MB")
    print(f"  payload hash = {phash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())