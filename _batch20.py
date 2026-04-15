"""批量运行 bot.py --dry-run --once，每次清状态，碰到报错立即停"""
import subprocess, sys, time, os
import io

BOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")
CLEAN = ["dry_run_bankroll.json", "trading_journal.csv", "bot_trades.xlsx"]

def clean():
    for f in CLEAN:
        try:
            os.remove(os.path.join(os.path.dirname(os.path.abspath(__file__)), f))
        except FileNotFoundError:
            pass

def sp(s):
    """安全打印到 stdout（Windows GBK 兼容）"""
    try:
        sys.stdout.write(s + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:
        sys.stdout.buffer.write((s + "\n").encode("utf-8", errors="replace"))
        sys.stdout.flush()

def run(i):
    clean()
    sp(f"\n=== Run {i}/20 ===")
    try:
        r = subprocess.run(
            [sys.executable, BOT, "--dry-run", "--once"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=600,
        )
        out = (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        sp(f"!!! Run {i} TIMEOUT !!!")
        return False
    except Exception as e:
        sp(f"!!! Run {i} EXCEPTION: {e} !!!")
        return False
    for line in out.splitlines():
        if any(kw in line for kw in ["[结算]", "[信号]", "[跳过]", "Traceback", "Error:", "Exception:"]):
            sp(line)
    if r.returncode != 0:
        sp(f"!!! Run {i} FAILED (exit {r.returncode}) !!!")
        sp(out[-3000:])
        return False
    for kw in ["Traceback", "Exception:", "Error:"]:
        if kw in out:
            sp(f"!!! Run {i} contains error keyword: {kw} !!!")
            return False
    sp(f"Run {i}/20 done (exit 0)")
    return True

if __name__ == "__main__":
    for i in range(1, 21):
        ok = run(i)
        if not ok:
            sp(f"\nStopped at Run {i}")
            sys.exit(1)
        time.sleep(2)
    sp("\n=== All 20 runs complete, no errors! ===")
