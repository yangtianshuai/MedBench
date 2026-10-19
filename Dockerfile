# 医院智能问数 · 服务镜像
#
# 设计取舍：
#   1. 单阶段 + slim 基础镜像：本服务没有编译型依赖（oracledb 1.4.2 有 cp311 轮子），
#      不需要 gcc，镜像可以压到 200MB 左右 —— 院内离线拷贝介质时体积是硬约束。
#   2. 非 root 运行：容器内被攻破也只能读，不能往镜像层写。
#   3. **不内置 Oracle Instant Client**：HIS 11.2 的 thick mode 需要院方提供的客户端
#      与授权文件，不能放进公开镜像。直连 HIS 时按 compose 里的注释挂载；
#      走 ADS 宽表（推荐默认）则完全不需要。
#   4. HEALTHCHECK 用 /api/health：它能同时验证"进程活着"和"路由可用"，
#      比只探端口更能提前发现启动期异常。

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 只带运行期真正需要的东西（docs/eval 也带上：运维要在容器内跑自测与对账脚本）
COPY app ./app
COPY engine ./engine
COPY semantic-layer ./semantic-layer
COPY eval ./eval
COPY scripts ./scripts
COPY README.md ./

# var/ 是唯一需要可写的目录（元数据库、审计库、运行时配置、ADS 宽表）
RUN mkdir -p /app/var \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status == 200 else 1)"

# ★ 默认绑定 0.0.0.0 必须配合非 demo 的鉴权模式：
#   安全闸门在 demo 模式下绑非环回地址会**拒绝启动**（这是刻意的，别去改它）。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
