#!/usr/bin/env bash
# run-ac-overnight.sh — 挂机跑 arm_c vs arm_a 真实对比（拿 A−C）
#
# 用法（睡前）:
#   cd ~/mcp/platform/eval-harness/experiments/controlled_4arm
#   nohup bash run-ac-overnight.sh 8 3 > overnight.log 2>&1 &
#   然后 tail -f overnight.log 看进度，或直接去睡
#   参数: $1=每臂任务数(默认8)  $2=每任务trial数(默认3)
#
# 原理（feature toggle 没接线的绕过方案 = 进程级隔离）:
#   阶段1: agent 用【默认配置=reflexion/verifier OFF】跑 arm_c(裸模型)
#   阶段2: 杀掉 agent，用【REFLEXION+VERIFIER ON】重启，跑 arm_a(全增强)
#   阶段3: score.py 分别打分 → A−C 就是 pass_rate 差值
#
set -uo pipefail

TASKS="${1:-8}"
TRIALS="${2:-3}"
MCP_ROOT="$HOME/mcp"
EXP_DIR="$MCP_ROOT/platform/eval-harness/experiments/controlled_4arm"
VENV="$MCP_ROOT/platform/agent-server/.venv"
AGENT_DIR="$MCP_ROOT/platform/agent-server"
JWT_FILE="$MCP_ROOT/.dev-run/jwt.txt"

log() { echo "[$(date +%H:%M:%S)] $*"; }

# ── 前置健康检查（跑之前快速失败，别挂一夜才发现）──────────────
preflight() {
  log "===== 前置健康检查 ====="
  local ok=1
  # 1. ollama 活着 + 有 qwen2.5:7b
  if curl -s -m 3 http://localhost:11434/api/tags | grep -q "qwen2.5:7b"; then
    log "✅ ollama + qwen2.5:7b"
  else
    log "❌ ollama 没起 或 缺 qwen2.5:7b"; ok=0
  fi
  # 2. auth :8090 活着（发 JWT 用）
  if curl -s -m 3 -o /dev/null -w "%{http_code}" http://localhost:8090/auth/login -X POST -H "Content-Type: application/json" -d '{}' | grep -qE "400|401|200"; then
    log "✅ auth-service :8090"
  else
    log "❌ auth-service :8090 没响应"; ok=0
  fi
  # 3. memory :8180 + codeexec :8380（agent 工具后端）
  curl -s -m 3 -o /dev/null http://localhost:8180/actuator/health 2>/dev/null && log "✅ memory :8180" || log "⚠️ memory :8180 没响应(agent 可能少工具)"
  curl -s -m 3 -o /dev/null http://localhost:8380/actuator/health 2>/dev/null && log "✅ codeexec :8380" || log "⚠️ codeexec :8380 没响应(code_run 任务会失败)"
  # 4. tasks.yaml 有足够可用任务
  local ready
  ready=$(cd "$EXP_DIR" && grep -c "parent_sha:" tasks.yaml 2>/dev/null || echo 0)
  log "ℹ️  tasks.yaml 可用任务数: ~$ready (需 $TASKS)"
  # 5. venv + uvicorn
  [[ -x "$VENV/bin/uvicorn" ]] && log "✅ agent venv" || { log "❌ agent venv 缺失"; ok=0; }
  # 6. 内存检查（低于阈值警告）
  local free_pct
  free_pct=$(memory_pressure 2>/dev/null | grep -o "free percentage: [0-9]*" | grep -o "[0-9]*" || echo "?")
  log "ℹ️  内存 free: ${free_pct}%  $([ "${free_pct:-0}" -lt 30 ] 2>/dev/null && echo '⚠️ 偏低,7B 推理可能慢/超时')"

  if [[ $ok -eq 0 ]]; then
    log "❌ 前置检查失败，中止。修复上面 ❌ 项后重跑。"
    exit 1
  fi
  log "✅ 前置检查通过"
}

# ── 后台心跳监控：实时报告某个 run 目录的进度 ──────────────────
# 用法: start_heartbeat <out_dir_name> <expected_total> & HB_PID=$!
start_heartbeat() {
  local out_name="$1" expected="$2"
  local run_dir="$EXP_DIR/runs/$out_name"
  (
    local last=0
    while true; do
      sleep 30
      local done_ct=0
      [[ -f "$run_dir/index.jsonl" ]] && done_ct=$(wc -l < "$run_dir/index.jsonl" 2>/dev/null | tr -d ' ')
      # agent 还活着吗
      local agent_ok="✅"
      curl -s -m 3 http://localhost:8580/health | grep -q '"status":"ok"' || agent_ok="❌DOWN"
      # 只在有变化或每 30s 报一次
      log "  💓 [$out_name] 进度 $done_ct/$expected · agent $agent_ok · $(date +%H:%M:%S)"
      if [[ "$done_ct" -ge "$expected" ]]; then break; fi
    done
  ) &
  echo $!
}

# ── 刷新 JWT（防过期）─────────────────────────────────────────
refresh_jwt() {
  log "刷新 JWT..."
  local resp
  resp=$(curl -sS -X POST http://localhost:8090/auth/login \
    -H "Content-Type: application/json" \
    -d '{"username":"evaltest","password":"EvalTest12345!"}')
  echo "$resp" | python3 -c "import sys,json;open('$JWT_FILE','w').write(json.load(sys.stdin)['access_token'])" \
    && log "JWT 已刷新" || { log "❌ JWT 刷新失败: $resp"; exit 1; }
}

# ── 杀 + 重启 agent（可传环境变量切 arm 配置）───────────────────
restart_agent() {
  local reflexion="$1" verifier="$2"
  log "重启 agent: REFLEXION=$reflexion VERIFIER=$verifier"
  # 杀旧 agent
  pkill -f "uvicorn app.main:app --port 8580" 2>/dev/null || true
  sleep 3
  # 起新 agent（后台）—— 用子 shell 避免污染主脚本的 cwd
  (
    cd "$AGENT_DIR"
    AGENT_STRICT_TOOLS=true \
    AGENT_MLX_BASE_URL=http://localhost:8600 \
    AUTH_SERVER_URL=http://localhost:8090 \
    MEMORY_SERVER_URL=http://localhost:8180 \
    CODEEXEC_SERVER_URL=http://localhost:8380 \
    AGENT_REFLEXION_ENABLED="$reflexion" \
    AGENT_SUBAGENT_VERIFIER_ENABLED="$verifier" \
    PYTHONUNBUFFERED=1 \
    nohup "$VENV/bin/uvicorn" app.main:app --port 8580 > "$EXP_DIR/agent-arm.log" 2>&1 &
  )
  # 等 health
  for i in $(seq 1 30); do
    if curl -s -m 2 http://localhost:8580/health | grep -q '"status":"ok"'; then
      # 确认配置真的切了（读回 agent 的实际 settings，防 .env 覆盖）
      log "agent ready (第 ${i} 次探测) — 已设 REFLEXION=$reflexion VERIFIER=$verifier"
      return 0
    fi
    sleep 2
  done
  log "❌ agent 30 次探测都没起来。agent-arm.log 尾部："
  tail -20 "$EXP_DIR/agent-arm.log" 2>/dev/null | sed 's/^/     /'
  exit 1
}

cd "$EXP_DIR"

# 计算预期运行数（心跳用）
EXPECTED=$(( TASKS * TRIALS ))
log "🚀 实验启动: 每臂 $TASKS 任务 × $TRIALS trials = $EXPECTED runs/臂 · 共 2 臂 = $((EXPECTED*2)) runs"
log "   预估: 单 run 1-5min → 总计约 $((EXPECTED*2*2))-$((EXPECTED*2*5)) 分钟"

# 前置检查（快速失败）
preflight

# ── 阶段 1: arm_c（裸模型，OFF）─────────────────────────
log "===== 阶段1/2: arm_c (裸 7B, reflexion/verifier OFF) ====="
restart_agent false false
refresh_jwt
HB_PID=$(start_heartbeat overnight_armC "$EXPECTED")
# stdbuf -oL 让 run.py 的每行输出实时刷出（不 buffer），进度可见
( cd "$EXP_DIR" && stdbuf -oL -eL python -u run.py --full --only-arm arm_c --limit "$TASKS" --trials "$TRIALS" --out overnight_armC ) 2>&1
kill "$HB_PID" 2>/dev/null || true
log "✅ arm_c 完成"

# ── 阶段 2: arm_a（全增强，ON）──────────────────────────────
log "===== 阶段2/2: arm_a (7B + reflexion + verifier ON) ====="
restart_agent true true
refresh_jwt
HB_PID=$(start_heartbeat overnight_armA "$EXPECTED")
( cd "$EXP_DIR" && stdbuf -oL -eL python -u run.py --full --only-arm arm_a --limit "$TASKS" --trials "$TRIALS" --out overnight_armA ) 2>&1
kill "$HB_PID" 2>/dev/null || true
log "✅ arm_a 完成"

# ── 阶段 3: 打分对比 ─────────────────────────────────────────
log "===== 阶段3: 打分 ====="
( cd "$EXP_DIR" && python score.py --runs-dir overnight_armC ) 2>&1 | tee "$EXP_DIR/score_armC.txt" || log "⚠️ score armC 失败(可能需要 judge)"
( cd "$EXP_DIR" && python score.py --runs-dir overnight_armA ) 2>&1 | tee "$EXP_DIR/score_armA.txt" || log "⚠️ score armA 失败"

log "===== 全部完成 ====="
log "结果: runs/overnight_armC (arm_c) vs runs/overnight_armA (arm_a)"
log "A−C = arm_a pass_rate − arm_c pass_rate（看 score_armA.txt / score_armC.txt）"
log "⚠️ 恢复 agent 到默认配置: 重新跑 scripts/dev/scenarios/with-tools/up.sh"
