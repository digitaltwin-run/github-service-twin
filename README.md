# GitHub Service Twin (`digitaltwin-run/github-service-twin`)

Fail-closed **GitHub Digital Twin** oraz silnik ciągłości pracy offline (`quota-deferred` / brak limitu GitHub API) z trwałym, łańcuchowanym dziennikiem outbox i rekoncyliacją do prawdziwego GitHuba po odblokowaniu limitów API.

## Dlaczego powstał ten projekt

Gdy pula zapytań GitHub API spada do zera (`X-RateLimit-Remaining: 0`, stan `publication_deferred` w `subactor/onedev-agent`), normalny przepływ pracy (tworzenie issue/PR, publikacja statusów `onedev/local-verify`, walidacja zmian) zatrzymuje się do czasu `reset_at`.

`github-service-twin` dostarcza lokalny bliźniak REST API v3 GitHuba oparty o **prawdziwe lokalne repozytoria Git** i bazę SQLite (WAL):

1. **Tryb pracy przy wyczerpanym API (`twin-offline` / `quota-deferred`)**:
   - Czyta prawdziwe gałęzie, commity, porównania (`compare/{base}...{head}`) i przodków z lokalnych repozytoriów Git.
   - Obsługuje tworzenie/odczyt issues, pull requestów, statusów commitów (`onedev/local-verify`) oraz recenzji niezależnego aktora walidującego.
   - Zapisuje każdą mutację w trwałym, hash-chained dzienniku `outbox_ops` z kluczem idempotencji i dokładnym zamrożonym `head_sha`.
   - Zachowuje **granicę aktorów (`wellmanifest/merge`)**: autor PR nie może zatwierdzić własnego PR, a lokalne scalenie bliźniacze wymaga `onedev/local-verify = success` oraz zatwierdzenia przez niezależną rolę `validator_app` na dokładnie tym samym `head_sha`.
2. **Tryb rekoncyliacji po odblokowaniu API (`sync`)**:
   - Monitoruje `X-RateLimit-Remaining` / `reset_at` (oraz opcjonalny plik `api-budget-github.json` z `subactor/onedev-agent`).
   - Gdy API GitHuba wraca powyżej rezerwy:
     - wypycha lokalne gałęzie ticketowe (`git push`) jeśli jeszcze ich brak na remote,
     - idempotentnie zakłada lub mapuje PR na prawdziwym GitHubie (`twin_pr -> upstream_pr`),
     - odtwarza statusy weryfikacji (`onedev/local-verify`) na dokładnym zamrożonym `head_sha`,
     - **nigdy nie uruchamia osobistego review ani merge** — zamiast tego przekazuje zmapowany PR do niezależnego adaptera Validator App (`run-local-direct-pr.sh --apply --merge`) i oznacza wpis jako `RECONCILED` dopiero po odczycie stanu z prawdziwego GitHuba.

## Powiązane projekty w ekosystemie (odnalezione przez `semcod/search`)

| Projekt | Rola w ekosystemie | Integracja z `github-service-twin` |
| --- | --- | --- |
| `subactor/onedev-agent` | Lokalny koordynator i executor CI (`GitHubClient`, `publication_deferred`) | Pełna kompatybilność REST v3 z `GitHubClient(api_url=...)`, odczyt `api-budget-github.json` |
| `subactor/validator-agent` | Niezależna bramka review i merge (`run-local-direct-pr.sh`) | Jedyny uprawniony cel przekazania merge przy rekoncyliacji upstream |
| `dockuri/procache` | Współdzielony cache odczytów SQLite i cooldown po 403/429 | Spójny model limitów i cooldownu bez sztormu zapytań |
| `uri-twin/uri-twin-core` i `uri-twin/uri-twin-forge` | Kontrakt `subactor.twin-fact/v1` i mapa zdolności forge | Endpoint `/twin/facts` emituje podpisane hashem fakty bliźniaka |
| `digitaltwin-run/{paypal,plesk,linkedin,youtube}-service-twin` | Konwencja lokalnych bliźniaków usług | Spójna struktura `/health`, port `8796`, tryb fail-closed |
| `semcod/planfile` i `dockuri/planfile-orchestration` | Kolejka ticketów i retry outbox dla GitHuba | Obsługa `/repos/{owner}/{repo}/issues` i idempotentnych kluczy ticketów |
| `urirun-connectors/urirun-connector-github` i `urirun-connector-forge` | Złącza Git/GitHub i kontrakty kwitów | Możliwość przekierowania bazowego URL API na lokalny bliźniak |

## Szybki start

```bash
# 1. Uruchom testy (w tym test zgodności z prawdziwym GitHubClient z subactor/onedev-agent oraz E2E sync)
make test

# 2. Uruchom lokalne demo całego cyklu:
#    brak API -> praca w Twinie -> odblokowanie API -> synchronizacja z upstream GitHub
make demo

# 3. Uruchom serwer lokalnego bliźniaka na porcie 8796
make serve
```

## Główne endpointy HTTP

- `GET /health` — stan zdrowia i tryb pracy bliźniaka
- `GET /twin/status` — szczegółowy stan limitów API, zarejestrowanych repozytoriów i outboxa
- `GET /twin/facts` — fakty zgodne z `subactor.twin-fact/v1` (bez żadnych sekretów)
- `GET /twin/outbox` — lista oczekujących i zrekoncyliowanych operacji
- `POST /twin/quota` — aktualizacja/symulacja stanu limitu upstream (`remaining`, `reset_at`, `blocked`)
- `POST /twin/sync` — uruchomienie rekoncyliacji kolejki outbox z upstream GitHub
- Standardowe endpointy GitHub REST v3: `/rate_limit`, `/repos/{owner}/{repo}`, `/repos/{owner}/{repo}/branches`, `/repos/{owner}/{repo}/git/ref/heads/{branch}`, `/repos/{owner}/{repo}/compare/{base}...{head}`, `/repos/{owner}/{repo}/issues`, `/repos/{owner}/{repo}/pulls`, `/repos/{owner}/{repo}/pulls/{n}/reviews`, `/repos/{owner}/{repo}/pulls/{n}/merge`, `/repos/{owner}/{repo}/statuses/{sha}`, `/repos/{owner}/{repo}/commits/{sha}/status(es)`.

Szczegóły architektury: [docs/architecture.md](docs/architecture.md).
