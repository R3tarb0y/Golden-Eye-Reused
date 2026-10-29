from control_center.db import bootstrap, interrupt_orphan_jobs

if __name__ == "__main__":
    bootstrap()
    count = interrupt_orphan_jobs(live_seconds=30)
    print(f"[CONTROL RESET] orphan active jobs -> INTERRUPTED: {count}")
