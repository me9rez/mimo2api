#!/usr/bin/env python3
"""从 Windows 上的 MiMo Desktop cookie 库导出 passToken 到 mimo_pass_token.json。

为什么需要这一步：Windows 上 MiMo Desktop 运行时会对 Cookies 库加独占锁
(ERROR_SHARING_VIOLATION)，脚本拷不出副本，所以必须先完全退出 Desktop 再导出。
导出一次后，mimo_bridge.py 直接读这个 JSON，之后 Desktop 开不开都不影响启动。

凭证优先级（mimo_bridge.py）：MIMO_PASS_TOKEN 环境变量 > 本目录 mimo_pass_token.json > cookie 库自动读取
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "mimo_pass_token.json"
ACCOUNT_HOST = "account.xiaomi.com"


def cookie_candidates() -> list[Path]:
    home = Path.home()
    cands = [
        home / "Library/Application Support/Xiaomi MiMo/Partitions/xiaomi-account/Cookies",
        home / "Library/Application Support/Xiaomi MiMo/Partitions/xiaomi-account/Network/Cookies",
        home / ".config/Xiaomi MiMo/Partitions/xiaomi-account/Network/Cookies",
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:  # Windows: %APPDATA%\Xiaomi MiMo\...（cookie 为明文 value，无需 DPAPI 解密）
        win_base = Path(appdata) / "Xiaomi MiMo" / "Partitions" / "xiaomi-account"
        cands += [win_base / "Network" / "Cookies", win_base / "Cookies"]
    return cands


def main() -> int:
    src = next((p for p in cookie_candidates() if p.exists()), None)
    if src is None:
        print("[ERR] 没找到 MiMo Desktop 的 cookie 库，路径候选：", file=sys.stderr)
        for p in cookie_candidates():
            print(f"      {p}", file=sys.stderr)
        return 1

    fd, tmp = tempfile.mkstemp(prefix="mimo2api-ck-", suffix=".db")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        shutil.copy2(src, tmp_path)
    except PermissionError:
        print(
            f"[ERR] cookie 库被独占锁定，读不到：{src}\n"
            "      请完全退出 MiMo Desktop（含托盘图标）后重跑本脚本。",
            file=sys.stderr,
        )
        return 2
    try:
        conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT name, value FROM cookies WHERE host_key = ?",
                ("." + ACCOUNT_HOST,),
            ).fetchall()
        finally:
            conn.close()
    finally:
        tmp_path.unlink(missing_ok=True)

    jar = {n: v for n, v in rows}
    if not jar.get("passToken"):
        print("[ERR] 库能读到但没有 passToken，请先在 MiMo Desktop 里登录一次。", file=sys.stderr)
        return 3

    cred = {
        "passToken": jar["passToken"],
        "userId": jar.get("userId"),
        "cUserId": jar.get("cUserId"),
    }
    OUT.write_text(json.dumps(cred, ensure_ascii=False), encoding="utf-8")
    # 只报告字段与长度，不打印凭证明文
    print(f"[OK] 已写入 {OUT}")
    print("     passToken 长度 %d，userId %s，cUserId %s" % (
        len(cred["passToken"]), cred["userId"] or "-", (cred["cUserId"] or "-")[:3] + "***"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
