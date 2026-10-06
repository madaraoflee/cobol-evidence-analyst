#!/bin/bash
cd -- "$(dirname -- "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then
    python3 build_app.py "$@"
    build_result=$?
else
    echo "请先安装 Python 3.12：https://www.python.org/downloads/"
    build_result=1
fi
echo
read -r -p "按回车关闭窗口……" answer
exit "$build_result"
