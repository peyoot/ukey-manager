"""PyInstaller 入口（避免入口脚本名 agent.py 与包 agent 同名）。"""
from agent.agent import main

if __name__ == "__main__":
    raise SystemExit(main())
