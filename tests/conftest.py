import sys
from pathlib import Path

# tests 内のモジュール同士（test_hardening が test_report のヘルパを使う）を import できるようにする
sys.path.insert(0, str(Path(__file__).resolve().parent))
