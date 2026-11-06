#!/usr/bin/env python3
"""上傳前的秘密掃描：檢查「會被 git 追蹤的檔案」有沒有 token、API key、Discord ID、email，
以及你本機 .env 裡的任何值（最可靠的一項：只要 .env 的值出現在要上傳的檔案裡就擋下）。

    python scripts/check_secrets.py            掃描目前所有被追蹤的檔案
    python scripts/check_secrets.py --staged   只掃描已 git add 的內容（pre-commit hook 用這個）
    python scripts/check_secrets.py --history  連同目前分支的整個 git 歷史一起掃（第一次公開前建議跑一次）

只用標準函式庫。有發現就印出「檔案:行號」與類別（不印出秘密本身）並以 1 結束。
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PATTERNS = {
    "Google API key": re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    "Discord bot token": re.compile(r"[MNO][A-Za-z0-9_\-]{23,26}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}"),
    "Discord ID（17~20 位數字）": re.compile(r"(?<![0-9])[0-9]{17,20}(?![0-9])"),
    "email 地址": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]*[a-z]{2,}"),
    "private key": re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    "其他常見金鑰": re.compile(r"(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|xox[bp]-[A-Za-z0-9-]{20,})"),
}
# 這些 email 是文件／範例用的，不算外洩
EMAIL_OK = re.compile(r"(@users\.noreply\.github\.com|@example\.(com|org)|noreply@anthropic\.com)$", re.I)
# 這些副檔名不掃（二進位、圖片）
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".onnx", ".bin", ".db", ".ogg", ".wav", ".pyc", ".ico"}
# .env 裡這些值太普通，比對會誤報
TRIVIAL = {"true", "false", "none", "0", "1", "08:00", "a2", "5.5", "10", "30", "8", "12", "450", "365"}


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, errors="replace").stdout


SENSITIVE_KEY = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD|WEBHOOK|CONTACT|(^|_)IDS?$)", re.I)


def env_values():
    """本機 .env 裡「名稱看起來是機密」的值（token、key、各種 ID、聯絡信箱），長度 ≥ 6。
    模型名稱、檔名、上限數字這類一般設定不比對，免得到處誤報而讓人習慣性無視警告。"""
    path = ROOT / ".env"
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip("\"'")
            if SENSITIVE_KEY.search(key) and len(value) >= 6 and value.lower() not in TRIVIAL:
                values[key] = value
    return values


def scan_text(label, text, secrets, findings):
    for n, line in enumerate(text.splitlines(), 1):
        for key, value in secrets.items():
            if value in line:
                findings.append(f"{label}:{n}  .env 的 {key} 的值出現在這裡")
        for name, pattern in PATTERNS.items():
            for m in pattern.finditer(line):
                if name == "email 地址" and EMAIL_OK.search(m.group(0)):
                    continue
                findings.append(f"{label}:{n}  疑似 {name}")


def main():
    staged, history = "--staged" in sys.argv, "--history" in sys.argv
    secrets = env_values()
    findings = []

    if staged:
        files = [f for f in git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines() if f]
        for name in files:
            if Path(name).suffix.lower() in SKIP_SUFFIX:
                continue
            scan_text(name, git("show", f":{name}"), secrets, findings)
    elif history:
        commits = git("rev-list", "HEAD").split()  # 目前分支的歷史（也就是你要 push 的那條）
        seen = set()
        for commit in commits:
            for name in git("ls-tree", "-r", "--name-only", commit).splitlines():
                blob = git("rev-parse", f"{commit}:{name}").strip()
                if blob in seen or Path(name).suffix.lower() in SKIP_SUFFIX:
                    continue
                seen.add(blob)
                scan_text(f"{commit[:8]}:{name}", git("show", blob), secrets, findings)
        authors = {a for a in git("log", "HEAD", "--format=%ae%n%ce").split() if a}
        findings += [f"git 歷史的作者/提交者信箱：{a}（會在 GitHub 上公開）" for a in sorted(authors) if not EMAIL_OK.search(a)]
    else:
        for name in git("ls-files").splitlines():
            path = ROOT / name
            if path.suffix.lower() in SKIP_SUFFIX or not path.is_file():
                continue
            scan_text(name, path.read_text(encoding="utf-8", errors="replace"), secrets, findings)

    if staged:  # 提醒（不擋）：commit 的作者信箱會跟著 push 公開
        email = git("config", "user.email").strip()
        if email and not EMAIL_OK.search(email):
            print(f"⚠️  提醒：目前 git 的 user.email 是 {email}，commit 後會公開在 GitHub。"
                  "想保護隱私可改用 GitHub 的 noreply 信箱：git config user.email "
                  "\"<你的ID>+<帳號>@users.noreply.github.com\"")

    mode = "已暫存的內容" if staged else "整個 git 歷史" if history else "所有被追蹤的檔案"
    if findings:
        print(f"❌ 掃描{mode}：發現 {len(findings)} 處可能外洩的資料")
        for f in findings[:60]:
            print("   ", f)
        print("請先處理再 commit / push（真的是誤報就修改內容，或調整 scripts/check_secrets.py）。")
        return 1
    print(f"✅ 掃描{mode}：沒有發現 token、API key、Discord ID、email，也沒有 .env 的值"
          f"（比對了 {len(secrets)} 個 .env 值）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
