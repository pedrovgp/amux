# Session: relay releases a deleted topic (thread_missing)

Repo: `/home/pv/Documents/pv/projetos/amux-telegram-gateway` (branch
`feat/telegram-gateway`). Dispatched by the general manager on the owner's
"sim, faça isso".

## 1. Relay must release a deleted topic

**Prompt**

> Você é o worker do projeto `amux-telegram-gateway` (/home/pv/Documents/pv/projetos/amux-telegram-gateway). Despacho do general-mgr a pedido do dono.
>
> PEDIDO DO DONO (verbatim): "sim, faça isso" — referente ao diagnóstico: ele deletou o tópico 544 "General manager dispatch workflow", mandou a mensagem no #General; a mensagem CHEGOU (`voice_transcribed ... route=dispatcher`), mas as respostas do GM ficaram no limbo: `relay_send_failed chat_id=-1003797327543 session=general-mgr-823440998 error=Message thread not found` repetido de 12:09:53 a 12:13:10. O GM só destravou chamando `POST /message`, que devolveu `502 {"error": "Message thread not found", "released": true}` e removeu o vínculo. Então a resposta final caiu no #General.
>
> TAREFA (corrigir na raiz, não contornar):
> 1. Investigue por que o caminho do relay não faz o mesmo release que o caminho de controle: `control.py` `/message` trata `thread not found` com `drop_session_topics` e loga `topic_released ... reason=thread_missing source=message`; o `/topic` connect também solta quando o tópico está DEAD; `bot.py` — os relay paths por volta das linhas 3911 e 4072 só logam `relay_send_failed` e fazem break/continue, sem soltar o vínculo. Confirme no código atual.
> 2. Implemente a correção no ponto de causa: quando um envio do relay falhar com "Message thread not found", solte o vínculo daquele (chat, sessão), deixe a mensagem pendente ser reenviada SEM thread (#General) em vez de falhar de novo, e registre em log uma linha grepável (`topic_released ... reason=thread_missing source=relay` ou equivalente). Não apague estado à mão, não mexa no Bot API direto, não toque em outros serviços.
> 3. Siga as regras do repo: leia `AGENTS.md` (todo bug fix: causa raiz + sinal no log), crie/atualize o progress file em `memory-bank/progress/` ANTES do código (plan-first), com testes unitários/de integração que provem: (a) falha de thread excluída solta o vínculo; (b) a resposta posterior vai para #General (thread=None) em vez de falhar de novo; (c) nenhuma regressão nos caminhos `/topic` e `/message`. Rode os testes relevantes.
> 4. Se precisar subir o fix para valer, reinicie SOMENTE `systemctl --user restart amux-telegram-gateway` — nada de outro serviço, tmux da frota ou worker — e verifique `systemctl --user show ... -p ActiveState` e `GET http://127.0.0.1:8834/health`. NÃO polua os tópicos do dono com mensagens de teste; prefira testes automatizados. Se não puder validar de ponta a ponta sem poluir, diga o que não validou.
> 5. Commite código + progress file juntos, no formato `<type>: ...` com `Progress: memory-bank/progress/<nome>.md` na mensagem, e inclua a atualização do prompt-history da sessão (§16). Sem `git add -A`: stage por nome.
>
> Formato: TODA sua resposta começa com a linha `—— answer ——` (exata, sozinha na linha), sem ela não chega ao tópico do Telegram. Objetivo, leitura no celular. Se algo falhar, mostre o erro exato; não contorne.

**Final answer**

—— answer ——

**Corrigido na raiz, testado e no ar.** Nenhuma mensagem de teste nos tópicos do dono.

**Causa raiz (confirmada no código):** a regra "este erro prova que o tópico morreu" morava só em `control.py`. `/message` e o reconnect do `/topic` soltavam o vínculo; os dois caminhos do relay (`_relay_api` e o pane) só logavam `relay_send_failed` e mantinham o vínculo — daí o limbo de 4 min até um humano chamar `/message`.

**Fix:**
1. `relay.thread_missing(exc)` — uma cópia só; `control.py` e `bot.py` importam dela.
2. `_send_relay_text`: ao ver "Message thread not found", solta o vínculo e loga `topic_released chat_id=… thread=… session=… reason=thread_missing source=relay path=api|pane` (WARNING, greppável).
3. Reenvia a MESMA resposta **uma vez** com `thread=None` e sem âncora (a âncora era mensagem do tópico morto) → cai no #General. Só então `oc_relayed_id`/`last_relayed` avançam: nada duplica, nada fica preso.
4. Se o reenvio falhar: `relay_send_failed … retried_without_thread=true`, mensagem continua pendente. Direitos/flood continuam não soltando nada.

**Testes:** `tests/test_relay_topic_release.py` (5 novos: api solta+entrega no General, pane idem, direitos não solta, retry tentado uma vez e fica pendente, predicado único). Suíte: **413 passed** (408 antes); `/topic` e `/message` seguem verdes.

**No ar:** restart só do gateway → `ActiveState=active`, `SubState=running`; `/health` → `{"status":"ok","service":"telegram-gateway"}`; `--check` → `OK` (`amux health ok commit=abfba334d4`, 15 sessões). Zero `relay_send_failed` desde o restart (12 são do incidente de ontem).

**Não validei ponta a ponta:** não havia tópico morto no estado (o vínculo do GM já tinha sido solto pelo `/message`), e não vou apagar tópico do dono para testar — a prova é a suíte. No próximo caso: `grep -a 'topic_released .*reason=thread_missing source=relay' ~/.amux/logs/telegram-gateway.log`.

**Limitação documentada:** tópico deletado ENTRE dois chunks de uma resposta longa faz o retry reenviar o texto inteiro (duplica o chunk já entregue) — janela de ms; perder a resposta seria pior.

**Commit:** `fix(telegram-gateway): relay releases a deleted topic and delivers in General` em `feat/telegram-gateway`, com `Progress: memory-bank/progress/tg-relay-thread-missing-release.md` e o prompt-history da sessão. Não pushei nada. Obs.: `memory-bank/` é gitignored mas force-added, como no commit anterior `f8cf229ad`; o `projectDecisions.md` §1 ainda diz para nunca stagear — está desatualizado em relação à prática de hoje (posso corrigir se quiser).

## 2. Corrigir decisões/commit/push + o "stalled" que continua chegando

**Prompt**

> [from Telegram @whoamiPV] É, pode corrigir, comitada, puxe, mas no tópico CCI ou CUSED eu continuo recebendo mensagem de que está stalled e eu estou evoluindo no terminal do meu computador, então tem alguma coisa estranha ainda.

**Final answer**

—— answer ——

**Feito: as duas coisas.**

**1) `projectDecisions.md` corrigido e commitado** (`e2715d418`): agora diz a regra real — `memory-bank/` segue no `.gitignore`, mas plano/progresso/prompt-history entram no MESMO commit com `git add -f`, como já acontecia desde `f8cf229ad`.

**2) O "stalled" repetido: causa achada, corrigida e no ar.**

O que eu medi (não é palpite):
- As linhas que chegavam são dos tópicos ligados a `ocusage` (thread 579) e `tg-fix-steering-stall` (thread 619); os nomes que o gateway guarda são "OCUsage custo por sessão" e "Fix steering stall" — se você renomeou algum para CCI/CUSED, é aí que apareciam.
- Cada linha é UM aviso sobre UMA mensagem: `queued=1`, `oldest_min=911` (15h), `last_skip=not-running`, `blocked_now=not-running`.
- E o `not-running` é verdade MEDIDA: dentro do pane do amux o opencode está suspenso (`[1]+ Stopped opencode --session …`) e o pane está no prompt do shell. Você está evoluindo no SEU terminal, mas o processo que o amux controla está parado — por isso a mensagem nunca chega por ali.

O defeito era duplo: o amux repete o WARN a cada hora (de propósito, é o log) e o gateway tratava cada hora como caso novo → uma mensagem presa virava ~10 avisos. Correção (`7cc949690`):
- a identidade agora é a LINHA (`last_skip_id`): mesma mensagem presa = 1 aviso, depois `stall_notice_suppressed`;
- mensagem nova (linha nova) avisa de novo;
- fila esvaziou → `stall_notice_cleared` no log e o próximo stall volta a avisar;
- fila ilegível → 1 aviso por CONDIÇÃO, nunca por hora;
- reinício/deploy não re-avisa o que você já viu: linha mais velha que o processo é "seedada" (`stall_notice_seeded`);
- o texto parou de mentir: lane parada agora diz "a lane NÃO ESTÁ RODANDO … a mensagem espera a lane iniciar; nada se perde e nenhum deadline força", em vez de "o terminal do worker mostra onde travou".

**Evidência ao vivo:** reiniciei só o gateway (11:08:58) → `ActiveState=active`, `running`, `/health` ok, e o log trouxe `stall_notice_seeded … session=ocusage … oldest_age_s=55249` e o mesmo para `tg-fix-steering-stall`. Ou seja: aquelas duas linhas não chegam mais. Suíte: **422 passed** (413 antes; 9 testes novos).

**O que deixei de propósito:** as duas mensagens continuam na fila. Descartá-las é decisão sua, e o repo tem um contra-exemplo documentado de por que `not-running` não é dead-letter por idade (2026-08-19: 41 lanes caídas 6,5h, tudo entregue no restart). Se quiser: (a) apago as duas (são retomadas do GM, já obsoletas) ou (b) inicio as lanes para entregá-las — diga qual.

**Pushed:** `1ba9a052b..e2715d418` → `origin/feat/telegram-gateway`.

**Pendências honestas:** o WARN das 12:00 deve sair como `suppressed`; se o servidor do amux reiniciar antes, `last_skip_id` (que é em memória) some e pode sair UM aviso extra, uma vez. A metade Rust do fix de steering da sessão anterior segue não deployada (só entra via `origin/main` + CI).

## 3. Apagar as duas linhas presas

**Prompt**

> [from Telegram @whoamiPV] apaga

**Final answer**

—— answer ——

**Apagadas — pelo caminho sancionado, não na mão.**

- `DELETE /api/workers/ocusage/steer {"id": …}` → `{"cleared":1,"ok":true,"spared_system":0}`
- `DELETE /api/workers/tg-fix-steering-stall/steer {"id": …}` → `{"cleared":1,"ok":true,"spared_system":0}`

Verificação ao vivo:
- `steering_queue` → **0** linhas;
- `GET /api/debug/steering` → `{"lanes": [], "measured": true, "n_considered": 0}`;
- o gateway fechou o ciclo sozinho às 13:26:33:
  `stall_notice_cleared … session=ocusage … reason=queue_empty` e o mesmo para `tg-fix-steering-stall`.

Ou seja: nada mais preso, nada vai repetir o aviso, e um stall NOVO volta a avisar uma vez (a dedupe foi limpa junto).

**Evidência preservada antes do delete:** ids e texto das duas linhas (as "Retomada (general-mgr)" de 25/11 19:48) ficaram em `memory-bank/progress/tg-stall-notice-dedupe.md`.

**Nada além disso foi tocado** — nenhum outro worker, nenhum tópico, e o único serviço reiniciado nesta sessão foi o próprio gateway (que já estava com o fix).

**Commit + push:** `cbda44d40` (docs com a evidência) → `origin/feat/telegram-gateway`.
