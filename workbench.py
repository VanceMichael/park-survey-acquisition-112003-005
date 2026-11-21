"""兼容入口：``python3 workbench.py --help``。"""
from joint_workbench.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
