# Architektura `github-service-twin`

## 1. Cel i problem operacyjny

W autonomicznej pętli dostarczania (`coding-agent` -> `onedev-agent` -> `validator-agent`) chwilowe wyczerpanie limitu GitHub API (`remaining = 0`, `publication_deferred`) blokuje nawet lokalnie weryfikowalne zadania.

`github-service-twin` rozdziela **lokalny postęp oparty o prawdziwy Git i lokalne CI** od **finalnej publikacji na GitHub.com**, nie naruszając przy tym żadnej granicy uprawnień.

## 2. Granice bezpieczeństwa i aktorów

1. **Brak sekretów w stanie bliźniaka**: nagłówki `Authorization` nigdy nie trafiają do SQLite, outboxa ani odpowiedzi HTTP. Bliźniak rozpoznaje wyłącznie role aktorów (`coding_agent`, `onedev_verifier`, `validator_app`) przez konfigurację mapy ról lub lokalne nagłówki testowe (`X-Twin-Actor`, `X-Twin-Role`).
2. **Twarda separacja ról (fail-closed)**:
   - Tylko aktor o roli `validator_app` i tożsamości różnej od autora PR może wystawić recenzję `APPROVED` dla zamrożonego `commit_id`.
   - Scalenie w bliźniaku (`PUT /pulls/{n}/merge`) wymaga jednocześnie: zgodnego `sha == head_sha`, statusu `onedev/local-verify == success` na tym `sha` oraz recenzji `APPROVED` od niezależnego `validator_app` na tym `sha`.
   - Bliźniak **nigdy nie podszywa się pod realne scalenie GitHub** i przy rekoncyliacji nie wywołuje osobistego `gh pr merge` ani `gh pr review` — zamiast tego deleguje zatwierdzenie i merge do niezależnego adaptera Validator App (`run-local-direct-pr.sh`) i weryfikuje zwrotny stan upstream.

## 3. Przepływ danych (Offline -> Reconcile)

```text
[GitHub API remaining == 0]
        │
        ▼
  QuotaGuard (mode = quota-deferred)
        │
        ├─► Coding Agent: otwiera PR / Issue w Twinie (na prawdziwym local git HEAD)
        ├─► OneDev Executor: weryfikuje exact head_sha i publikuje onedev/local-verify w Twinie
        ├─► Twin Validator: weryfikuje granice i zapisuje gotowość w hash-chained outbox_ops
        │
[GitHub API odblokowane: remaining > reserve && now >= reset_at]
        │
        ▼
  Reconciler (github-service-twin sync --apply)
        │
        ├─► 1. git push gałęzi ticketowej (jeśli brak na upstream)
        ├─► 2. Idempotentne ensure_pull_request -> mapa twin_pr -> upstream_pr
        ├─► 3. Odtworzenie statusu onedev/local-verify na zamrożonym head_sha
        ├─► 4. Przekazanie do niezależnego Validator App (run-local-direct-pr.sh)
        └─► 5. Odczyt upstream (merged == true) -> oznaczenie outbox jako RECONCILED
```

## 4. Kontrakt faktów `subactor.twin-fact/v1`

Endpoint `GET /twin/facts` udostępnia niezmienny, deterministycznie zahashowany (`payload_sha256`) zbiór faktów bliźniaka, zgodny z filozofią `uri-twin/uri-twin-core`:
- brak surowych URL-i z poświadczeniami,
- jawny status trybu (`quota-deferred` vs `live-ready`),
- liczniki oczekujących i zrekoncyliowanych wpisów outboxa oraz zarejestrowanych repozytoriów.
