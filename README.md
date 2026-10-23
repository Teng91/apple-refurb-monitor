# Apple 台灣整修品 Telegram 監控器

Google Apps Script 每小時觸發 GitHub Actions 檢查 Apple 台灣官方整修品商店，偵測新上架、下架、降價和漲價。只有變化時才傳 Telegram，單次通知會彙總各類變化數量，價格異動會顯示舊價 → 新價。

## 部署

1. 在 GitHub 建立一個新 repository，把本專案全部檔案推上去。
2. 在 Telegram 聯絡 `@BotFather`，輸入 `/newbot` 建立 Bot，取得 Bot Token。
3. 開啟自己的 Bot 並按 **Start**（或傳一則訊息給它）。
4. 在瀏覽器開啟：

   ```text
   https://api.telegram.org/bot<你的 BOT TOKEN>/getUpdates
   ```

   找到回傳資料中的 `message.chat.id`，這就是 Chat ID。
5. GitHub repository → **Settings → Secrets and variables → Actions → Secrets**，新增：

   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

6. 前往 **Actions → Apple Refurbished Monitor → Run workflow** 手動執行一次。第一次會建立基準資料並傳送啟動通知；此後只有商品變化才通知。

> 不要把 Bot Token 寫進程式或貼到公開訊息。若曾曝光，請到 BotFather 立刻撤銷並重建 Token。

## 可選篩選設定

在 GitHub 的 **Settings → Secrets and variables → Actions → Variables** 加入以下任意變數：

| Variable | 範例 | 說明 |
|---|---|---|
| `APPLE_CATEGORIES` | `mac,ipad` | 分類：`mac,ipad,airpods,homepod,appletv,accessories` |
| `INCLUDE_KEYWORDS` | `MacBook Pro,M5` | 名稱或 SKU 至少包含其中一項 |
| `EXCLUDE_KEYWORDS` | `Studio Display` | 排除包含任一關鍵字的商品 |
| `MAX_PRICE_TWD` | `60000` | 只追蹤不超過此台幣價格的商品 |

多個關鍵字用半形逗號分隔，不分英文大小寫。篩選條件變更後，第一次執行可能會將被排除的舊商品顯示為下架；若不希望收到這批通知，可以刪除 `data/state.json` 後手動執行，重新建立基準。

## 本機測試

不需要安裝第三方套件：

```bash
python3 -m unittest discover -s tests -v
DRY_RUN=true python3 apple_refurb_monitor.py
```

## Google Apps Script 外部排程

`google_apps_script/Code.gs` 可以每小時呼叫 GitHub `workflow_dispatch`，避免單純依賴 GitHub Actions 排程。Apps Script 專案需要：

1. 在 Script Properties 設定 `GITHUB_TOKEN`，Token 僅授予此 repository 的 Actions 寫入權限。
2. 手動執行 `triggerAppleRefurbMonitor` 驗證 GitHub workflow 可成功啟動。
3. 執行 `installHourlyTrigger` 建立唯一的每小時觸發器。

注意：Apps Script 的每小時觸發時間可能在小時內浮動，不保證固定分鐘。本專案不再使用 GitHub 原生 `schedule`，避免重複觸發。

### 每日 Health 通知

執行一次 `installDailyHealthTrigger`，加入台北時間每天午夜附近的獨立觸發器（Google `nearMinute(0)` 約有正負 15 分鐘誤差，GitHub 排隊可能再延遲）。它呼叫 `triggerDailyHealthCheck`，帶入 `health_check: true`；每小時一般檢查不發 Health。

午夜檢查成功後，即使沒有商品異動，也會收到 Telegram Health 通知，包含本次成功檢查時間與追蹤商品數。檢查失敗時只發錯誤通知，不發健康成功通知。這證明本次流程成功，不代表過去 24 小時每次排程都成功；若 Google 排程本身停止，Health 也不會送出。

## 注意事項

- Apps Script 每小時觸發器不保證在固定分鐘執行。
- Telegram 單則訊息有長度限制；大量商品同時變動時會自動分成續篇，每則仍會保留完整變動數量。
- Apple 調整頁面資料格式時，程式會停止並發錯誤通知，不會把全部商品誤報為下架。
