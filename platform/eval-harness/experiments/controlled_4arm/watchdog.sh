#!/usr/bin/env bash
# watchdog.sh — 实验实时监控（借鉴 ML 训练圈的外置监控 + 看门狗）
#
# 独立于实验进程运行。每 INTERVAL 秒报告：
#   - 进度: 已完成任务数 / blinded 产物数
#   - 心跳: agent 最后一次 LLM loop 距今多久（判断是否 hang）
#   - 当前: agent 正在跑第几 loop、调了什么工具、累计错误
#   - 资源: 内存 free% / ollama 是否在推理
#   - 告警: agent 无响应 / 心跳超时 / 内存危险
#
# 用法（另开一个终端，和实验并行）:
#   bash watchdog.sh                 # 默认监控间隔 30s
#   bash watchdog.sh 15              # 15s 间隔
#
# 设计原则（ML 圈标准）:
#   1. 监控进程独立于被监控进程（实验挂了 watchdog 还活着才能报警）
#   2. 只读不写（绝不干预实验，纯观测）
#   3. 心跳判活（N 秒无新 loop 日志 = 疑似 hang，高亮告警）

set -uo pipefail

INTERVAL="${1:-30}"
MCP_ROOT="$HOME/mcp"
EXP_DIR="$MCP_ROOT/platform/eval-harness/experiments/controlled_4arm"
AGENT_LOG="$EXP_DIR/agent-arm.log"          # overnight 脚本重启 agent 时写这
FALLBACK_LOG="$MCP_ROOT/.dev-run/agent.log" # 常规 dev stack 的 agent 日志
HANG_THRESHOLD=200                          # agent 超过这么多秒无新 loop = 告警

C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_CYN=$'\033[36m'; C_RST=$'\033[0m'

pick_log() {
  # 用最近被修改的那个 agent 日志
  if [[ -f $AGENT_LOG && ( ! -f $FALLBACK_LOG || $AGENT_LOG -nt $FALLBACK_LOG ) ]]; then
    echo "$AGENT_LOG"
  elif [[ -f $FALLBACK_LOG ]]; then
    echo "$FALLBACK_LOG"
  else
    echo ""
  fi
}

# 日志最后一条含时间戳的行，距今多少秒（macOS 日志是 HH:MM:SS.mmm 开头）
seconds_since_last_log() {
  local log="$1"
  [[ -z $log || ! -f $log ]] && { echo "-1"; return; }
  local last_ts
  last_ts=$(grep -oE '^[0-9]{2}:[0-9]{2}:[0-9]{2}' "$log" 2>/dev/null | tail -1)
  [[ -z $last_ts ]] && { echo "-1"; return; }
  local now_s log_s
  now_s=$(date +%s)
  # 今天的日期 + 日志时间 → epoch
  log_s=$(date -j -f "%Y-%m-%d %H:%M:%S" "$(date +%Y-%m-%d) $last_ts" +%s 2>/dev/null || echo "$now_s")
  echo $(( now_s - log_s ))
}

echo "${C_CYN}🐕 watchdog 启动（间隔 ${INTERVAL}s，hang 阈值 ${HANG_THRESHOLD}s）。Ctrl-C 停止。${C_RST}"
echo "监控目录: $EXP_DIR/runs/"
echo ""

while true; do
  TS=$(date +%H:%M:%S)
  LOG=$(pick_log)

  # ── 进度：数各 run 目录的 blinded 产物 ──
  progress=""
  for d in "$EXP_DIR"/runs/overnight_armC "$EXP_DIR"/runs/overnight_armA; do
    if [[ -d $d/blinded ]]; then
      n=$(ls "$d/blinded"/*.md 2>/dev/null | wc -l | tr -d ' ')
      progress+="$(basename "$d")=${n}产物  "
    fi
  done
  [[ -z $progress ]] && progress="(还没产出)"

  # ── agent 健康 ──
  if curl -s -m 3 http://localhost:8580/health 2>/dev/null | grep -q '"status":"ok"'; then
    agent_state="${C_GRN}agent OK${C_RST}"
  else
    agent_state="${C_RED}agent 无响应!${C_RST}"
  fi

  # ── 心跳：距上次 loop 日志多久 ──
  since=$(seconds_since_last_log "$LOG")
  if [[ $since -lt 0 ]]; then
    hb="${C_YEL}无日志${C_RST}"
  elif [[ $since -gt $HANG_THRESHOLD ]]; then
    hb="${C_RED}⚠️ ${since}s 无新loop(疑似hang)${C_RST}"
  else
    hb="${C_GRN}${since}s前活动${C_RST}"
  fi

  # ── 当前在干啥：日志里最后一条 loop / tool 行 ──
  cur=""
  if [[ -n $LOG && -f $LOG ]]; then
    cur=$(grep -aE "🧠 LLM call|🤖 LLM decided|Tool result|consecutive:" "$LOG" 2>/dev/null | tail -1 | sed -E 's/^[0-9:.]+ \[[^]]*\] [A-Z]+ +//' | cut -c1-90)
  fi
  [[ -z $cur ]] && cur="(暂无 loop 日志)"

  # ── 资源 ──
  mem=$(memory_pressure 2>/dev/null | grep -oE "free percentage: [0-9]+%" | grep -oE "[0-9]+%")
  ol_cpu=$(ps aux | grep "llama-server" | grep -v grep | awk '{print $3}' | head -1)
  [[ -z $ol_cpu ]] && ol_cpu="?"

  # ── 输出一行状态 ──
  echo "${C_CYN}[$TS]${C_RST} $agent_state | 心跳:$hb | 内存free:${mem:-?} | ollama-cpu:${ol_cpu}%"
  echo "         进度: $progress"
  echo "         当前: $cur"
  echo ""

  sleep "$INTERVAL"
done
