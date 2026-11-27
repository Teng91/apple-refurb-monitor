# Apple 台灣整修品 Telegram 監控器

每小時檢查一次 [Apple 台灣官方整修品商店](https://www.apple.com/tw/shop/refurbished)，有新上架、下架、降價或漲價就傳 Telegram 通知你；每天午夜另外傳一則健康通知，讓你知道它還在運作。

不需要伺服器、全部免費：Google Apps Script 負責排程，GitHub Actions 負責檢查，Telegram Bot 負責通知。

**需要準備**：GitHub 帳號、Google 帳號、Telegram。照著下面的[設定](#設定)做大約 15 分鐘，不需要會寫程式。想讓 AI 助理（例如 Claude Code）幫你做，見 [docs/ai-setup.md](docs/ai-setup.md)。

## 會收到哪些通知

| 通知 | 什麼時候 |
|---|---|
| 🍎 整修品更新 | 有新上架、下架、降價或漲價時，同一次的變化彙總成一則 |
| 💚 Health | 每天台北時間 00:00 左右，附上目前追蹤幾項商品；有分類讀取異常會用 ⚠️ 列出來 |
| ⚠️ 執行失敗 | 所有分類都讀不到、GitHub Variables 填錯（訊息會寫是哪個設定）、`data/state.json` 損壞，或通知已傳出但快照沒能存回 repository 時 |
| ✅ 已啟動 | 第一次執行、建立基準資料時 |

**哪天午夜沒收到 Health 通知，就代表有地方壞了**，見[疑難排解](#疑難排解)。

## 設定

### 1. 建立 Telegram Bot

1. 在 Telegram 聯絡 `@BotFather`，輸入 `/newbot`，取得 Bot Token。
2. 開啟自己的 Bot 並按 **Start**。
3. 用瀏覽器開啟下面的網址（`bot` 和 Token 中間沒有空格、不用 `<>`），回傳資料裡的 `message.chat.id` 就是 Chat ID：

   ```text
   https://api.telegram.org/bot<你的 BOT TOKEN>/getUpdates
   ```

   看到 `{"ok":true,"result":[]}` 的話，再傳一則訊息給 Bot、重新整理即可。

<details>
<summary>想傳到群組或頻道</summary>

- **群組**：把 Bot 加進群組，在群組傳 `/start@你的Bot帳號`（Bot 看不到一般訊息），再開一次 `getUpdates` 找 `message.chat.id`。群組的 Chat ID 是負數（例如 `-1001234567890`），**負號要一起填**。
- **頻道**：把 Bot 設成頻道管理員，發一則貼文，到 `getUpdates` 找 `channel_post.chat.id`。

</details>

> Bot Token 不要寫進程式或貼到公開的地方；萬一曝光，到 BotFather 撤銷重發。

### 2. 設定 GitHub

1. 按右上角 **Fork**，再到你 fork 的 **Actions** 頁面按啟用（fork 預設停用 Actions）。fork 一定是公開的；想要私人 repository，改用 [github.com/new/import](https://github.com/new/import) 匯入並選 **Private**（會用到 Actions 免費分鐘數，每月約 750 分鐘，免費額度 2,000）。
2. **刪掉 `data/state.json`**（網頁上點開檔案 → 🗑️ → Commit）。那是原作者的商品快照，留著會收到一堆無關的變化通知。
3. **Settings → Secrets and variables → Actions → Secrets**，新增 `TELEGRAM_BOT_TOKEN` 與 `TELEGRAM_CHAT_ID`。
4. （選用）在同一頁的 **Variables** 設定[篩選條件](#篩選條件)。
5. **Actions → Apple Refurbished Monitor → Run workflow** 手動執行一次，應該會收到「✅ 已啟動」。勾選 `health_check` 再跑一次，可以看 Health 通知的樣子。

> **之後不要按「Sync fork」**：會把原作者的 `data/state.json` 帶回來。要更新程式，手動複製 `apple_refurb_monitor.py`（和有改到的 `.github/workflows/`、`Code.gs`）就好。不小心同步了，就再刪一次 `data/state.json` 並手動執行一次。

### 3. 設定 Google Apps Script 排程

1. 到 [script.google.com](https://script.google.com) 建立專案，貼上 `google_apps_script/Code.gs` 的全部內容，**把開頭的 `GITHUB_REPOSITORY` 改成你的 `帳號/repository名稱`**。
2. 建立 GitHub token：**Settings → Developer settings → Personal access tokens → Fine-grained tokens**，只選這個 repository，權限只給 **Actions: Read and write**。到期日設一年內，**並在行事曆記下到期日**：token 過期的唯一症狀是午夜不再收到 Health 通知。
3. Apps Script 的 **專案設定 → 指令碼屬性**，新增 `GITHUB_TOKEN`，值貼上剛才的 token。
4. 函式選單選 `triggerAppleRefurbMonitor` → **執行**，到 GitHub Actions 頁面確認多了一次執行。第一次會要求授權，看到「Google 尚未驗證這個應用程式」是正常的（腳本是你自己建的），點 **進階 → 前往（不安全）→ 允許**。
5. 執行 `installMonitorTrigger`，建立「每小時檢查」和「每天 00:00 健康檢查」兩個觸發器。

<details>
<summary><code>Code.gs</code> 開頭的其他設定</summary>

| 設定 | 預設 | 說明 |
|---|---|---|
| `GITHUB_REF` | `main` | 在哪個分支上執行 workflow |
| `MONITOR_INTERVAL_HOURS` | `1` | 幾小時檢查一次（只接受 1、2、4、6、8、12） |
| `HEALTH_HOUR` | `0` | 每天台北時間幾點傳健康通知（0–23） |
| `DISPATCH_RETRY_DELAYS_SECONDS` | `[10, 30, 60]` | GitHub 暫時錯誤時，每次重試前等幾秒 |

改完要再執行一次 `installMonitorTrigger`。真正在跑的是 Apps Script 裡那份，repository 裡的 `Code.gs` 只是副本。

</details>

## 篩選條件

在 GitHub 的 **Settings → Secrets and variables → Actions → Variables** 加入任意變數：

| Variable | 範例 | 說明 |
|---|---|---|
| `APPLE_CATEGORIES` | `mac,ipad` | 要追蹤的分類，預設全部：`mac,ipad,homepod,appletv,accessories` |
| `INCLUDE_KEYWORDS` | `MacBook Pro,M5` | 名稱或 SKU 至少包含其中一項 |
| `EXCLUDE_KEYWORDS` | `Studio Display` | 排除包含任一關鍵字的商品 |
| `MAX_PRICE_TWD` | `60000` | 只追蹤不超過這個價格的商品（整數，可加千分位逗號） |

多個關鍵字用半形逗號分隔，不分大小寫。改了篩選條件後，新被排除的商品會被當成「下架」、新納入的商品會被當成「新上架」通知；不想收到的話，先刪 `data/state.json` 再手動執行一次。

## 疑難排解

**午夜沒收到 Health 通知**：到 GitHub 的 **Actions** 頁面看 00:00 左右有沒有執行。

- **沒有執行**：到 Apps Script 的「執行項目」看錯誤，最常見是 `GITHUB_TOKEN` 過期（401），重新產生後更新指令碼屬性。
- **有，但失敗**：點進那次執行看 log。
- **有，而且成功**：檢查兩個 Telegram Secrets、Bot 有沒有被封鎖；群組的話確認 Chat ID 有負號、Bot 還在群組裡。

想馬上測整條流程，在 Apps Script 執行 `triggerDailyHealthCheck` 即可。

**一次收到很多「下架」或「新上架」**：多半是剛改了篩選條件，見上面。

**同樣的變化通知收到兩次**：上一次執行在傳到一半時失敗（會另外收到「執行失敗」），下一次會把整批變化重傳一次。寧可重複也不會漏掉。

**「執行失敗」說快照沒能 push 回 repository**：通知已經傳出去，但 `data/state.json` 沒存回去，修好之前每小時都可能重複收到同樣的變化。點訊息裡的連結看 log，常見原因是有人手動改了 `data/state.json` 造成衝突，或 repository 的 Actions 沒有寫入權限（**Settings → Actions → General → Workflow permissions**）。

**Health 通知出現「讀取失敗，沿用上次的 N 項商品」**：偶爾一次是暫時的網路問題；連續幾天都有，可能是 Apple 改了頁面結構，需要更新程式。

**Health 通知出現「Apple 沒有這個分類的頁面（404）」**：Apple 拿掉了這個分類，可以從 `APPLE_CATEGORIES` 拿掉它。

---

運作細節、安全性說明、本機測試：見 [docs/development.md](docs/development.md)。
