#!/bin/sh
# pack.sh —— 打包代码包（不含二进制）
#
# 教训：以前直接在工作目录里 rm 掉 tools/ 和 bin/ 再 zip，
# 结果把工作目录自己的文件删了，还打出了空 tools/。
# 现在一律复制到临时目录再压缩，工作目录一个字节都不动。

set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
# 输出位置：第一个参数；默认放在当前目录（以前的默认值是作者手机上的一个路径）
OUT="${1:-$PWD/luraph_v151_code.zip}"
case "$OUT" in /*) ;; *) OUT="$PWD/$OUT" ;; esac
command -v zip >/dev/null 2>&1 || { echo "[pack] 需要 zip 命令（apt install zip / apk add zip）" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "[pack] 复制代码到临时目录…"
cd "$SRC"
# 只带源码与文档；二进制、反编译器、示例样本都不进代码包。
# 文件清单取自 git（已跟踪 + 未忽略的新文件），这样 devm/、deobf/ 的子目录也完整；
# 不在 git 工作区里（比如解压出来的 zip）就退回到按扩展名查找。
list_files() {
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git ls-files -co --exclude-standard
    else
        find . -type f \( -name '*.py' -o -name '*.lua' -o -name '*.luau' -o -name '*.md' \
            -o -name '*.txt' -o -name '*.sh' -o -name '*.patch' -o -name '.gitignore' \
            -o -name 'LICENSE*' \) ! -path './.git/*' ! -path '*/__pycache__/*' \
            ! -path './.build/*' | sed 's|^\./||'
    fi
}
list_files | grep -v -E '^(bin|tools|examples|out_release|deobf/bin)/' \
           | grep -v -E '\.(exe|zip|pyc|bin|part)$' \
           | while IFS= read -r f; do
    [ -f "$f" ] || continue
    mkdir -p "$TMP/$(dirname "$f")"
    cp "$f" "$TMP/$f"
done

echo "[pack] 检查必带文件："
for f in deob.py dynlift.py lift.py fetch_bin.py build_x86.py \
         make_release.py fastsolve.py mkfast.py bufemu.lua robstub.lua \
         readability.py report.py pretty.py pack.sh \
         humanize.py reslot.py inlinevm.py luaubin.py luasandbox.py deobf_plus.py \
         libs/deobf_plus.py libs/beautify2.py libs/readable3.py libs/luascope.py \
         libs/luaunum.py; do
    if [ -f "$TMP/$f" ]; then
        printf '  ✓ %-24s %8s 字节\n' "$f" "$(stat -c%s "$TMP/$f")"
    else
        printf '  ✗ 缺少 %s\n' "$f"
        exit 1
    fi
done

echo "[pack] 压缩…"
rm -f "$OUT"
(cd "$TMP" && zip -qr "$OUT" .)
echo "[pack] 完成：$OUT  $(stat -c%s "$OUT") 字节"
echo "[pack] 内容："
unzip -l "$OUT" | tail -20
