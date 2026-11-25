# Cloud Run へのデプロイ

Go と Python を別サービスとして、同じ GCP プロジェクトにデプロイします。Cloud Run は両方ともリクエストベース課金、最小インスタンス数 0 で構成します。以下は新規のデモ環境の例です。

## GCP プロジェクトを準備

gcloud CLI にログインし、請求先を設定済みのプロジェクトを用意します。

```sh
PROJECT=your-project-id
REGION=asia-northeast1
REPOSITORY=shift-creator
QUEUE=shift-solve
WEB=shift-web
OPTIMIZER=shift-optimizer
SERVICE_ACCOUNT=shift-web-runtime@$PROJECT.iam.gserviceaccount.com

gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com firestore.googleapis.com cloudtasks.googleapis.com \
  secretmanager.googleapis.com --project "$PROJECT"

gcloud artifacts repositories create "$REPOSITORY" --repository-format=docker \
  --location "$REGION" --project "$PROJECT"
gcloud firestore databases create --database='(default)' --type=firestore-native \
  --location "$REGION" --project "$PROJECT"
gcloud tasks queues create "$QUEUE" --location "$REGION" --project "$PROJECT" \
  --max-concurrent-dispatches=1 --max-attempts=8 --min-backoff=5s --max-backoff=30s

gcloud iam service-accounts create shift-web-runtime --project "$PROJECT"
for role in roles/datastore.user roles/cloudtasks.enqueuer; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:$SERVICE_ACCOUNT" --role "$role"
done
```

Cloud Build で使うビルド用サービスアカウントには、Artifact Registry の対象リポジトリへの書き込み権限が必要です。Firestore が既にある場合は、利用する Native モードのデータベースを確認してください。既定以外の場合は Go に `FIRESTORE_DATABASE` を指定します。

Secret Manager に `shift-admin-token` と `shift-task-token` を作り、互いに異なるランダムな値を設定します。値は16文字以上です。OpenAI 連携も使う場合は `shift-openai-key` を作ります。各 Secret のアクセス権は Go の実行サービスアカウントに設定します。

```sh
for secret in shift-admin-token shift-task-token; do
  gcloud secrets add-iam-policy-binding "$secret" --project "$PROJECT" \
    --member "serviceAccount:$SERVICE_ACCOUNT" --role roles/secretmanager.secretAccessor
done
```

## ビルドとデプロイ

リポジトリのルートから実行します。Python は既定で PuLP と HiGHS を使います。下のコマンドでは `OPTIMIZER_SOLVER=pulp` を明示します。固定サンプルを再生する場合は `demo` を指定できます。

```sh
TAG=$(git rev-parse --short HEAD)
PREFIX=$REGION-docker.pkg.dev/$PROJECT/$REPOSITORY
WEB_IMAGE=$PREFIX/web:$TAG
OPTIMIZER_IMAGE=$PREFIX/optimizer:$TAG

gcloud builds submit . --project "$PROJECT" --config cloudbuild.yaml \
  --substitutions "_WEB_IMAGE=$WEB_IMAGE,_OPTIMIZER_IMAGE=$OPTIMIZER_IMAGE"

gcloud run deploy "$OPTIMIZER" --image "$OPTIMIZER_IMAGE" \
  --region "$REGION" --project "$PROJECT" --no-allow-unauthenticated \
  --cpu-throttling --min-instances=0 --max-instances=2 \
  --concurrency=1 --timeout=75s --cpu=1 --memory=512Mi \
  --set-env-vars OPTIMIZER_SOLVER=pulp

gcloud run services add-iam-policy-binding "$OPTIMIZER" \
  --region "$REGION" --project "$PROJECT" \
  --member "serviceAccount:$SERVICE_ACCOUNT" --role roles/run.invoker

OPTIMIZER_URL=$(gcloud run services describe "$OPTIMIZER" \
  --region "$REGION" --project "$PROJECT" --format 'value(status.url)')
NUMBER=$(gcloud projects describe "$PROJECT" --format 'value(projectNumber)')
PUBLIC_URL=https://$WEB-$NUMBER.$REGION.run.app

gcloud run deploy "$WEB" --image "$WEB_IMAGE" \
  --region "$REGION" --project "$PROJECT" --allow-unauthenticated \
  --service-account "$SERVICE_ACCOUNT" \
  --cpu-throttling --min-instances=0 --max-instances=2 --timeout=90s \
  --set-env-vars "GCP_PROJECT=$PROJECT,GCP_TASKS_LOCATION=$REGION,GCP_TASKS_QUEUE=$QUEUE,PUBLIC_URL=$PUBLIC_URL,OPTIMIZER_URL=$OPTIMIZER_URL" \
  --set-secrets "DEMO_ADMIN_TOKEN=shift-admin-token:latest,TASK_CALLBACK_TOKEN=shift-task-token:latest"
```

ブラウザで `PUBLIC_URL` を開き、ユーザー名 `admin` と `shift-admin-token` の値でログインします。Web サービスの公開 HTTP 入口ではアプリが管理者認証を行います。Cloud Tasks のコールバックは別トークンで認証され、Go が Cloud Run のサービス ID で Python を呼びます。公開アクセスに組織ポリシーがある場合は、入口の構成を調整してください。

OpenAI を有効にするには、実行サービスアカウントに `shift-openai-key` へのアクセス権を付与し、Web サービスに `OPENAI_API_KEY=shift-openai-key:latest` を `--update-secrets` で設定します。必要に応じて `--update-env-vars OPENAI_MODEL=モデルID` も指定します。

## ジョブと費用の確認

- Web と Optimizer のヘルスチェックは `GET /health` です。Optimizer には Cloud Run のサービス認証が必要です。Cloud Run は末尾が `z` の一部のパスを予約しています。履歴画面で生成が完了したことと、結果の採用ができることを確認します。 [Cloud Run の予約パス](https://docs.cloud.google.com/run/docs/known-issues#reserved-url-paths)
- タスク登録に失敗したジョブは永続化され、ジョブの照会時に登録を再試行します。ログに配送待ちが続く場合は、キュー、権限、コールバックURLを確認します。コールバック用ヘッダーで `POST /internal/jobs/redrive` を呼ぶと、未登録のジョブをまとめて再送できます。
- Cloud Tasks、Firestore、イメージ保存、ビルド、外向き通信には個別の料金条件があります。イメージの自動クリーンアップや予算通知は環境に合わせて設定してください。

[Cloud Run のサービス URL](https://docs.cloud.google.com/run/docs/triggering/https-request) · [Cloud Run のサービス間認証](https://docs.cloud.google.com/run/docs/authenticating/service-to-service) · [Cloud Run の料金](https://cloud.google.com/run/pricing)
