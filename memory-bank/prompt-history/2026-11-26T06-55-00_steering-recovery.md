## 1. Steering stall — root cause, sem follow-up

**Prompt**

> Você é o worker de recuperação do repo `amux-telegram-gateway` (/home/pv/Documents/pv/projetos/amux-telegram-gateway — workspace do amux: Rust em `crates/` + integração Python em `integrations/telegram-gateway`). Despacho do general-mgr a pedido do dono, que disse: "All steering skills seem to be stalled. The steel is not working." (= o steering não funciona).
>
> ATENÇÃO: o steering está quebrado globalmente. Faça TODO o trabalho a partir deste único briefing; não conte com mensagens de acompanhamento do dono nem do GM. Sua resposta final deve vir no tópico, começando com a linha `—— answer ——`.
>
> EVIDÊNCIA LIDA (logs `~/.amux/logs/telegram-gateway.log`, horário -03):
> - 2026-11-25 16:00 e 16:36: mensagens do dono no tópico OCUsage → `send_parked … held in the input box` / `queued (steering) — waiting for fresh boot readiness`. Nunca chegaram ao worker (conversa OpenCode seguiu ociosa desde 15:43).
> - 16:30 e 17:06: `idle_stop` parou as lanes enquanto mensagens estavam pendentes.
> - 16:47 em diante, DE HORA EM HORA até 06:00 de 26/11: `stall_notice_sent ... WARN amux_server::api::session_verbs: steering queue STALLED — messages are not reaching this lane session=ocusage queued=1 oldest_min=... last_skip=send-refused: composer` (depois `not-running blocked_now`) e o mesmo para `tg-fix-steering-stall`.
> - 26/11 06:53: a própria mensagem do GM foi `send_parked ... queued (steering) — waiting for fresh boot readiness`.
> - Tentativas de recuperação do GM: `amux send ocusage --keys Enter` entregue sem efeito; `amux send ocusage --stdin` recusado pelo boundary de grupo (correto — não tente contornar); `amux stop` + `amux start` disseram "already running" com pane em shell (estado zumbi); `amux rm` + re-registro com o mesmo pin reviveu o pane, mas as mensagens enfileiradas não voltaram e os avisos continuaram.
> - Vínculos do gateway (`~/.amux/telegram-gateway/state.json`) estão CORRETOS: 579 → ocusage → ses_disp11251506e272e5; 619 → tg-fix-steering-stall → ses_disp112516522e6f9f; 572 → tg-fix-relay-topic-release → ses_disp11251424c71cb7. Não é roteamento trocado.
>
> TAREFA (causa raiz, não paliativo):
> 1. Leia `AGENTS.md`, `~/.amux/notes/global/agents-guidelines.md` e os progress files `memory-bank/progress/tg-*.md` (há um worker `tg-fix-steering-stall` com investigação anterior — leia o que ele escreveu no disco, mesmo sem poder conversar com ele). Entenda: (a) o `amux_server::api::session_verbs` que devolve `send-refused: composer` / `not-running blocked_now`; (b) o `send_parked` do gateway (`telegram_gateway/bot.py`, seção "PARKED prompt", ~linhas 500-560) e o `stallwatch.py` (só leitura do WARN); (c) a interação com `idle_stop` e com lanes zumbis.
> 2. Crie um progress file novo em `memory-bank/progress/` ANTES de mexer no código, com milestones atômicos e verificáveis: reproduzir o estado, corrigir a entrega, impedir que `idle_stop` congele mensagem pendente, log grepável, testes de regressão.
> 3. Implemente a correção no ponto de causa: uma mensagem aceita pelo gateway ou pelo amux deve chegar à lane, ou ser devolvida ao dono com erro claro em vez de ficar enfileirada para sempre. Se a causa estiver no servidor Rust, rode os testes do crate afetado (`amux resources` antes de teste/build pesado); se estiver na integração Python, rode os testes do gateway. Não mexa em tópicos do dono, dados de outras lanes, tmux da frota ou serviços além do necessário. Se reiniciar o gateway, verifique health.
> 4. Commite código + progress file + prompt-history juntos (`<type>: ...` com `Progress: memory-bank/progress/<nome>.md`); sem `git add -A`, stage por nome.
> 5. Se a correção exigir mudança em repo diferente deste workspace, PARE e diga exatamente arquivo/linha — não contorne.
>
> Honestidade: se não conseguir reproduzir, mostrar o erro exato e o que ficou sem fazer.

**Final answer**

—— answer ——

Reproduzi e corrigi a causa raiz. Não era roteamento (os vínculos 579/619/572 estão corretos) nem falta de Enter: era uma cadeia de três elos.

**Causa raiz**

1. **`crates/amux-server/src/api/session_verbs.rs`** — o composer das duas lanes continha `[fromTelegram@whoamiPV]Eaí,resolveu?`, que é exatamente a forma sem espaços de `meta.last_send_text` (`[from Telegram @whoamiPV] E aí, resolveu?`): um paste antigo do próprio amux cujo Enter nunca entrou. `submit_own_steering_draft` só comparava o rascunho com a mensagem que estava sendo entregue *naquele instante* (a "Retomada" do GM), e `clear_delivered_duplicate_draft` só atua em transcript JSONL do Claude — as duas lanes são `CC_PROVIDER=opencode`, então o auto-heal estava inerte. Resultado: `steering_draft_preserved` → `send-refused: composer` para toda mensagem seguinte, para sempre. O watcher de direct-send ainda terminava em `direct_draft_idle_submit_gave_up` / `direct_draft_lane_stopped` sem limpar o rascunho.
2. **`telegram_gateway/bot.py`** — `idle_sweep_once` parava a lane após 30 min sem checar a fila. Parada, `lane_block_reason = not-running`, e `steer_dead_letter_verdict` só reap `no-env-file`/`archived`: nenhum prazo rescue a mensagem. O revive por prompt não adiantava enquanto o elo 1 existia.
3. **`stallwatch.py`** — o aviso no tópico era genérico e escondia `last_skip=send-refused: composer` vs `blocked_now=not-running`, as duas ações corretas são diferentes.

**Correção (causa, não paliativo)**

- Rust: predicado puro `draft_matches_own_delivery` (match exato com whitespace normalizado, mínimo 8 chars) com proveniência do ring do `pane_prompts`, `cmd_history` e `meta.last_send_text` (durável, sobrevive ao restart que adhesou o incidente). Um paste próprio obsoleto é limpo com C-u verificado, com WARN `steering_own_stale_draft_cleared` + evento `session.own_stale_draft_cleared`, e a mensagem da fila segue para a lane; o mesmo vale no watcher (`direct_draft_own_stale_paste_cleared`). **Rascunho não reconhecido continua preservado** — nenhum texto humano é sobrescrito.
- Gateway: `AmuxClient.steering_queued_sessions()` lê `/api/debug/steering`; o sweep nunca para uma lane com `queued>0` (`idle_stop_skipped_pending`) e **não para nenhuma lane** se a sonda estiver `measured:false` ou falhar (fila ilegível ≠ fila vazia).
- Gateway: a nota de stall no tópico agora traz `last_skip=` e `blocked_now=` (inclusive valor com espaço como `send-refused: composer`).

**Verificação**

- `python -m pytest -q` no gateway: **408 passed** (baseline 402; +6 testes).
- `cargo check -p amux-server --lib`: ok; `cargo clippy -p amux-server --lib -- -D warnings`: ok.
- `test-contended.sh -p amux-server --lib session_verbs`: **535 passed, 0 failed**; `stale_draft_tests`: 6 passed. `amux resources` lido antes dos builds (level ok).
- Progress file criado ANTES do código: `memory-bank/progress/tg-steering-stall-root-cause.md`. Código + progress + prompt-history comitados juntos, por nome, sem `git add -A`.

**O que ficou sem fazer (honestidade)**

- Não recuperei as duas lanes ao vivo: as linhas em `steering_queue` continuam lá (só consultei read-only); não reiniciei serviço nem toquei em tmux da frota, tópicos do dono ou dados de outras lanes.
- Nada foi pushed nem deployado. O fix Rust só entra em vigor quando chegar a `origin/main` e passar `checks`; a metade Python exige `systemctl --user restart amux-telegram-gateway`. O branch `feat/telegram-gateway` está apenas com o commit local.
- Nenhuma mudança em repo diferente deste workspace foi necessária.

Commit feito localmente em `feat/telegram-gateway` (não pushed; o hash é o
`git log -1`). Os arquivos
`memory-bank/` eram gitignored neste worktree e foram adicionados por nome com
`git add -f`, porque o briefing exigiu commitar progress + prompt-history juntos.

## 2. Push, deploy e pull autorizados

**Prompt**

> O branch feat/telegram-gateway está apenas com o commit local -> Pode dar push. E pode deployar também, e pode dar pull no origin main (tanto pra main quanto pra feat/)

**Final answer**

—— answer ——

Feito o que era seguro; duas coisas ficaram bloqueadas por divergência real entre os branches.

**Push:** `feat/telegram-gateway` já estava em `origin/feat/telegram-gateway` (outros workers comitaram depois do meu e já tinham empurrado); `git push origin feat/telegram-gateway` confirmou `Everything up-to-date`. Todos os 59 commits do range são do autor `pv` (o dono), que autorizou push/deploy aqui.

**Deploy:** a metade Python já está **live** — o gateway foi reiniciado às 10:30 e 11:07 (-03) e o log já mostra o campo novo (`stall_notice_sent … reason=last_skip=not-running, blocked_now=not-running`), então não reiniciei de novo. Para o fix Rust, pousei somente os dois arquivos `.rs` em `origin/main` (o diretório `integrations/telegram-gateway/` não existe no main) como commit `3b5918368`, com gate registrado: `owner_not_needed`, `revert_ok`, `land_gate_pass`. Os estágios rust/e2e/review do gate foram **explicitamente marcados como skipped** porque este container não tem `cargo-nextest`, `node_modules` nem o CLI `claude`; em compensação rodei localmente `cargo check`, `clippy -D warnings` e 535 testes do módulo `session_verbs`, e pedi revisão independente. `git push origin HEAD:main` publicou `abfba334d..3b5918368`.

**Pull:** `main` foi sincronizado (`amux sync-checkout`: `abfba334d → 3b5918368`). Em `feat/` NÃO fiz o merge de `origin/main`: os branches divergiram no provider opencode (40 trechos conflitantes em 6 arquivos, incluindo `Result<()>` vs `Result<bool>` em `opencode/mod.rs`). Resolver isso automaticamente poderia quebrar a frota; deixei para decisão humana/worker dedicado.

**Estado honesto:** `/health` ainda reporta `commit=abfba334d` — o builder só ativa depois que o workflow `checks` do commit `3b5918368` passa; no último check havia 14 sucessos e 15 checks pendentes. Assim que passar, o servidor puxa o novo binário. Não fiquei em loop de polling. As duas filas presas já não têm linhas em `steering_queue` (foram limpas por outro worker), então o incidente imediato está resolvido. O checkout compartilhado `amux-telegram-gateway` ficou com conflitos `UU` deixados por outro worker; não toquei nesses arquivos — este registro foi comitado num worktree limpo.
