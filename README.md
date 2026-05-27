# 夏季休暇調整アプリ MVP

## 概要

このアプリは、スタッフ・レジデントの夏季休暇希望を収集し、初回締切時に全希望を同時評価します。
早いもの順ではありません。

初回判定後も追加で休暇希望を入力できますが、既存の仮確定・確定予定や既存不在とコンフリクトする場合は通知されます。

## 実装済み機能

- ユーザー登録
- レジデントの期間別役割登録
  - チーフ
  - 臨床レジデント
  - 病理レジデント
- 夏季休暇以外の不在入力
  - 年休、出張、学会、外勤など
- 夏季休暇希望入力
  - 第1〜第5希望
  - 合計10勤務日
  - 土日・登録済み非勤務日は除外
- 初回締切時の一括判定
  - 申請順によらない同時評価
- コンフリクト検出
  - スタッフ重複
  - チーフ不足
  - チーフ＋臨床レジデント不足
  - チーフ＋臨床＋病理レジデント不足
- 仮確定・正式確定
- 締切後の随時判定
- Slack Webhook通知
- SMTPメール設定欄
- SQLite保存

## 実行方法

```bash
cd summer_vacation_app
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Windowsの場合:

```powershell
cd summer_vacation_app
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

## 院内サーバでの運用時に追加すべき事項

- 院内認証、LDAP、Active Directory、SSO等との連携
- HTTPS化
- アクセスログ・監査ログ
- PostgreSQL化
- バックアップ
- Slack Botまたは院内メールサーバ設定
- ユーザー権限の分離
  - 一般ユーザー
  - 管理者
- 非勤務日の一括登録
- 操作ログの保存

## 現在の制約

- 認証は未実装です。MVPではユーザーを選択して入力します。
- SQLiteを使用しています。小規模テスト用です。
- 複雑な公平性スコアは実装していません。
- コンフリクトがある場合は自動で勝者を決めません。
