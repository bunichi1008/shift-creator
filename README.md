# Shift Creator

介護施設1拠点の月間シフトを作る応募作品。Go が管理画面・API・ジョブ管理を担当し、Python が割当の検証とソルバー実行を担当します。日本語の入力は OpenAI API で条件案に変換し、管理者が確認してから登録します。

現在の `demo` バックエンドは、合成データ用の勤務表を読み込み、現在の必須条件で検査します。実際の最適化を始めるには、後述の Python アダプターに PuLP モデルを接続します。

## ローカルで試す

Go 1.24 以上と Python 3.12 が必要です。リポジトリのルートで仮想環境を作ります。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install './optimizer[test]'
```

ターミナル1で Python を起動します。

```sh
OPTIMIZER_SOLVER=demo PYTHONPATH=optimizer .venv/bin/python \
  -m uvicorn shift_optimizer.api:create_app --factory --host 127.0.0.1 --port 8001
```

ターミナル2で Go を起動します。

```sh
cd web
go run .
```

[http://localhost:8080](http://localhost:8080) で「シフト生成」を押すと、サンプル勤務表を検査してプレビューします。「勤務案に採用」すると編集できるようになります。夜勤のセルを休みに変更して固定し、もう一度生成すると、サンプルの割当が固定条件に違反する箇所を確認できます。

勤務条件は「条件を追加」から入力できます。日本語の提案を使う場合は、Go の起動環境に `OPENAI_API_KEY` を設定してください。モデルは `OPENAI_MODEL` で指定でき、既定値は `gpt-4o-mini` です。提案を作るときに送るのは、発話、対象期間と職員・勤務・資格の名称と ID です。フォームで確定すると入力の版が進みます。合成データでの動作確認を想定しています。

Docker がある環境では `docker compose up --build` でも起動できます。ローカルの入力版と実行履歴は通常起動なら `.shift-creator/`、Compose なら `demo-data` ボリュームに保存します。

### データの入力と出力

- 初期画面に合成の職員24名、勤務種別、人員枠、希望を用意しています。「データ設定」から職員・人員枠・希望の CSV をそれぞれ入れ替えられます。
- 勤務種別・期間・前月の履歴を変更する場合は、設定 JSON を保存し、編集して再インポートしてください。職員 ID・日付などを変えた場合は、参照する条件・履歴も合わせて更新します。
- 「CSV 出力」から、編集中の勤務案を出力できます。ファイルの文字コードは UTF-8 です。

## PuLP を接続する

Python の `solve(request: SolveRequest) -> SolveResult` を実装し、`OPTIMIZER_SOLVER=shift_optimizer.pulp_solver:solve` のように設定して Python サービスを起動します。`optimizer/shift_optimizer/models.py` の Pydantic 型を使います。環境変数を指定しない場合、計算結果は「最適化エンジン未接続」となります。

- `request.problem` に対象期間、職員、勤務、人員需要、希望、追加条件、固定、比較元の勤務案、前期間の履歴が入ります。`request.time_limit_seconds` をソルバーの時間上限に使います。
- `assignments` には実際の勤務だけを返します。休みは勤務の欠落で表現し、固定休の入力は `shift_id: "off"` で表現します。
- 結果は `optimal`（最適性を確認）・`feasible`（実行可能な候補あり）・`infeasible`（実行不能を証明）・`no_solution`（制限内に候補なし）を区別します。取得できる場合は目的値・境界・ギャップを添えます。
- 採用できる勤務表は Python が必須条件を独立検証します。日をまたぐ勤務、連勤、人員・資格、勤務不可、固定勤務を検証します。勤務時間は勤務開始月に計上し、前期間の勤務は休息・連勤の計算に使います。

`contracts/solve-request.schema.json` と `contracts/solve-result.schema.json` が JSON の契約です。`python -m shift_optimizer schema request` または `schema result` で再生成できます。`python -m shift_optimizer solve request.json` でスケジューラー単体を実行できます。

## 検証

```sh
(cd web && go test -race ./...)
PYTHONPATH=optimizer .venv/bin/python -m unittest discover -s optimizer/tests -v
scripts/smoke.sh
```

OpenAI 連携はモックでテストできます。実際のモデルを比較する場合は、12月のサンプルを読み込んだ状態で `.venv/bin/python scripts/evaluate_llm.py --allow-api-calls` を実行します。15件の入力についてルールの完全一致、曖昧な入力への確認、誤提案、応答時間を集計します。実行は API 利用量を消費します。

## デプロイ構成

同じリポジトリから Go と Python のコンテナを個別に作ります。Cloud Run の Go が Firestore に現在の入力版と各ジョブの入力スナップショットを保存し、Cloud Tasks のコールバックを受けて、IAM 認証付きで非公開の Python サービスを呼びます。Python は入力を検証してソルバーを実行します。Go は実行権を版付きで管理し、二重配送や古い結果の採用を防ぎます。

[Cloud Run デプロイ手順](docs/cloud-run.md)
