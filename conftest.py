"""根 conftest：为所有测试包提供统一的 fixture 基础。

确保 workspace 下各 src 目录在测试运行时可被 import（uv 已通过 editable
安装处理，此文件作为兜底）。
"""
