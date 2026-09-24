# Apple 台灣整修品 Telegram 監控器

每 15 分鐘在 GitHub Actions 檢查 Apple 台灣官方整修品商店，偵測新上架、下架和價格變化，只有變化時才傳 Telegram。

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

## 注意事項

- GitHub 的排程可能延遲數分鐘，不保證準點。
- 公開 repository 若長期沒有活動，GitHub 可能停用排程；定期確認 Actions 狀態。
- Apple 調整頁面資料格式時，程式會停止並發錯誤通知，不會把全部商品誤報為下架。
