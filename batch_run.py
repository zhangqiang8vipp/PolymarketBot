"""
批量运行 bot.py --dry-run --once，每完成一次记录结果，碰到报错立即停。
"""
import subprocess, sys, time, os, shutil

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")
CLEAN_FILES = ["dry_run_bankroll.json", "trading_journal.csv", "bot_trades.xlsx"]

def clean():
    for f in CLEAN_FILES:
        try:
            os.remove(os.path.join(os.path.dirname(os.path.abspath(__file__)), f))
        except FileNotFoundError:
            pass

def run_once(i):
    clean()
    start = time.time()
    print(f"\n{'='*60}\n[Run {i}/20] 开始\n{'='*60}", flush=True)
    result = subprocess.run(
        [sys.executable, SCRIPT, "--dry-run", "--once"],
        capture_output=True, text=True,
    )
    elapsed = time.time() - start
    output = result.stdout + result.stderr
    print(output[-2000:], flush=True)  # 最后2000字符
    if result.returncode != 0:
        print(f"\n!!! Run {i} 失败 (exit {result.returncode}) !!!", flush=True)
        return False
    # 检测关键错误关键词
    for kw in ["Traceback", "Error:", "Exception:", "CRITICAL"]:
        if kw in output:
            print(f"\n!!! Run {i} 含错误关键词: {kw} !!!", flush=True)
            return False
    print(f"[Run {i}/20] 完成 耗时={elapsed:.0f}s", flush=True)
    return True

def main():
    for i in range(1, 21):
        ok = run_once(i)
        if not ok:
            print("\n遇到问题，停止批量运行", flush=True)
            sys.exit(1)
        # 间隔2秒
        time.sleep(2)

if __name__ == "__main__":
    main()
