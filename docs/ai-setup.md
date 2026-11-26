# 交給 AI 助理設定

把這份文件交給能執行指令的 AI 助理（例如 Claude Code），跟它說「照 `docs/ai-setup.md` 幫我建好」。它能做的部分大約一半，剩下標 👤 的步驟只能由你本人在網頁或手機上操作。整個流程你大約要動手 10 分鐘。

| 步驟 | 誰做 | 說明 |
|---|---|---|
| 安裝並登入 `gh` | 👤 你 | 登入要在瀏覽器確認 |
| 建 Telegram Bot、傳訊息給 Bot | 👤 你 | BotFather 只能在 Telegram 裡對話，沒有 API |
| 查 Chat ID | 🤖 AI | 呼叫 Telegram `getUpdates` |
| Fork、啟用 Actions、刪 `state.json` | 🤖 AI | `gh`、`git` |
| 設 Secrets、Variables | 🤖 AI | `gh secret set`、`gh variable set` |
| 第一次執行、確認結果 | 🤖 AI | `gh workflow run`、`gh run watch` |
| 改 `Code.gs` 的 repository 名稱 | 🤖 AI | 改好、commit，給你可以直接貼上的內容 |
| 建 fine-grained token | 👤 你 | GitHub 沒有建立這種 token 的 API |
| Apps Script 貼程式碼、填 token、授權、裝觸發器 | 👤 你 | Google 的授權畫面一定要本人點 |
| 確認排程有自動執行 | 🤖 AI | 一小時後用 `gh run list` 檢查 |

## 給 AI 助理的規則

- **不要**在 `monitor.yml` 加 `schedule:` 來省掉 Apps Script，GitHub 原生的 `schedule` 常延遲好幾小時甚至整次跳過，兩種同時用又會重複觸發。
- **不要**執行 `gh repo sync` 或按「Sync fork」，同步會把原作者的 `data/state.json` 帶回來，害下一次執行傳一堆無關通知。
- **不要**請使用者把 Bot Token 或 GitHub token 貼進對話。Bot Token 照下面的做法從檔案讀取，指令和輸出都不要印出 Token；GitHub token 只貼進 Apps Script，不經過 AI。
- 下面的指令是 bash（Windows 請用 Git Bash；macOS 沒有 `python` 指令的話換成 `python3`）。`<你的帳號>` 換成使用者的 GitHub 帳號。
- 每一步做完先確認成功，再往下做。**全部完成的條件見最後一步**，在那之前不要說「已經設定好了」。

## 步驟

**1. 👤 準備 `gh` 和 Telegram Bot**

- 安裝 [GitHub CLI](https://cli.github.com/)，執行 `gh auth login` 登入。
- 照[建立 Telegram Bot](../README.md#1-建立-telegram-bot) 第 1、2 步建立 Bot、按 **Start**。要傳到群組的話，照那一節的群組步驟在群組裡傳 `/start@你的Bot帳號`。
- 把 Bot Token 存成**家目錄**下的 `tg_token.txt`（不要放進 repository 資料夾），內容只有 Token 一行。家目錄在 Windows 是 `C:\Users\你的使用者名稱`，在 macOS 是 `/Users/你的使用者名稱`。Windows 用記事本存檔時，「存檔類型」選「所有檔案」，否則容易存成 `tg_token.txt.txt`，AI 會讀不到。

**2. 🤖 查 Chat ID**

```bash
curl -s "https://api.telegram.org/bot$(cat ~/tg_token.txt)/getUpdates" \
  | python -c "import json,sys; r=json.load(sys.stdin)['result']; print({(u.get('message') or u.get('channel_post'))['chat']['id'] for u in r if u.get('message') or u.get('channel_post')})"
```

印出空的 `set()` 就請使用者再傳一則訊息給 Bot，然後重試。有好幾個 ID 時，正數是個人聊天、負數是群組或頻道，問使用者要哪一個。

**3. 🤖 Fork 並設定預設 repository**

```bash
gh repo fork Teng91/apple-refurb-monitor --clone --default-branch-only
cd apple-refurb-monitor
gh repo set-default <你的帳號>/apple-refurb-monitor
```

`gh repo set-default` 一定要做：fork 出來的資料夾有兩個遠端，不設的話後面的 `gh` 指令可能會跑去操作原作者的 repository。

使用者想要私人 repository 的話，改成：

```bash
git clone https://github.com/Teng91/apple-refurb-monitor.git
cd apple-refurb-monitor
git remote remove origin
gh repo create apple-refurb-monitor --private --source . --remote origin --push
gh repo set-default <你的帳號>/apple-refurb-monitor
```

**4. 🤖 啟用 Actions**（只有 fork 需要，私人 repository 預設就是啟用的）

```bash
gh api -X PUT repos/<你的帳號>/apple-refurb-monitor/actions/permissions -F enabled=true
gh workflow enable monitor.yml
gh workflow enable test.yml
```

失敗的話，👤 請使用者到 repository 的 **Actions** 頁面按綠色按鈕啟用。

**5. 🤖 刪掉原作者的快照**

```bash
git rm data/state.json
git commit -m "chore: remove upstream product snapshot"
git push origin main
```

**6. 🤖 設定 Secrets 和篩選條件**

```bash
gh secret set TELEGRAM_BOT_TOKEN < ~/tg_token.txt
gh secret set TELEGRAM_CHAT_ID --body "<第 2 步的 Chat ID>"
```

先問使用者要追蹤什麼，再照[篩選條件](../README.md#篩選條件)設定，例如：

```bash
gh variable set APPLE_CATEGORIES --body "mac,ipad"
gh variable set MAX_PRICE_TWD --body "60000"
```

**7. 🤖 第一次執行**

```bash
gh workflow run monitor.yml
sleep 10
gh run watch "$(gh run list --workflow monitor.yml --limit 1 --json databaseId -q '.[0].databaseId')" --exit-status
```

成功後使用者應該會收到「✅ Apple 整修品監控已啟動」，請使用者確認。再執行一次 `gh workflow run monitor.yml -f health_check=true`，確認會收到「💚 Health」通知。最後執行 `git pull`，把機器人 commit 的新 `data/state.json` 拉回來。

**8. 🤖 改 `Code.gs`**

把 `google_apps_script/Code.gs` 開頭的 `GITHUB_REPOSITORY` 改成 `'<你的帳號>/apple-refurb-monitor'`，commit 並 push。然後告訴使用者這個檔案的路徑，或直接把完整內容給他，讓他下一步貼上。

**9. 👤 建 GitHub token、設定 Apps Script**

照[設定 Google Apps Script 排程](../README.md#3-設定-google-apps-script-排程) 第 1–5 步做，其中第 1 步貼上 AI 在第 8 步改好的 `Code.gs`。重點：

- token 只選這個 repository，權限只給 **Actions: Read and write**。
- token 貼在 Apps Script 的指令碼屬性 `GITHUB_TOKEN`，**不要貼給 AI**。
- 執行 `triggerAppleRefurbMonitor` 時會跳出「Google 尚未驗證這個應用程式」，點 **進階 → 前往（不安全）→ 允許**。
- 最後執行 `installMonitorTrigger`。

**10. 🤖 確認完成**

```bash
gh run list --workflow monitor.yml --limit 5
```

全部符合才算完成：

- [ ] 使用者執行 `triggerAppleRefurbMonitor` 之後，列表裡多了一次成功的執行。
- [ ] **大約一小時後**，沒有人手動操作，列表裡又自動多了一次執行（代表 Apps Script 的觸發器有在跑）。
- [ ] 使用者有收到「✅ 已啟動」和「💚 Health」兩則 Telegram 通知。
- [ ] 刪掉 `~/tg_token.txt`（Token 已經存在 GitHub Secrets 了）。

最後提醒使用者：在行事曆記下 GitHub token 的到期日，到期前要換新。

