# defi-grid

Base 鏈上 **Uniswap v3** 的網格交易**唯讀分析與模擬**工具。目前是路線圖的**階段 1~2**:讀鏈上資料、用歷史價格回測,
把手續費、滑點、價格衝擊與 gas 都算進成本,回答「這組網格參數扣完成本還賺不賺」。

> **這個專案不碰錢包。** 沒有任何簽章、私鑰、API 金鑰或送出交易的程式碼,全部是唯讀。
> 模擬與分析結果不構成投資建議,也不代表實際會獲利。

## 技術棧
- Python 3.9+,**只用標準庫**(`urllib`、`json`、`unittest`),無第三方依賴
- 鏈上資料:Base 公開 RPC(預設 `https://mainnet.base.org`),自己用 `eth_call` 解 ABI,不依賴 web3 函式庫
- 歷史價格:GeckoTerminal 公開 API(免金鑰),取池子每小時 OHLCV,快取在 `cache/`
- 逐筆成交:鏈上 Swap 事件(`eth_getLogs`)。`mainnet.base.org` 不支援這種查詢,`swaps` 指令預設改用 `https://base-rpc.publicnode.com`

## 安裝與環境設定
不需要安裝任何套件。

```bash
cd ~/www/defi-grid
python3 -m unittest discover -s tests     # 跑測試
```

公開節點有速率限制;若常被限流,可改用自己的節點(金鑰只放環境變數,**不要寫進程式碼或提交到 git**):
```bash
export BASE_RPC_URL="https://你的節點網址"
```

## 使用方式
```bash
# 1. 讀池子:價格、流動性、不同金額的價格衝擊
python3 -m defigrid pool

# 2. 實測 Base 上一次 swap 的 gas 成本,含 L1 資料費(用來校準 --gas-usd)
python3 -m defigrid gas

# 3. 一組參數每個買賣循環是賺還是虧(可離線,給 --price 就不連網)
python3 -m defigrid economics --lower 2115 --upper 2585 --grids 20 --capital 1000 --price 2350

# 4. 用最近 30 天歷史價格回測
python3 -m defigrid backtest --days 30 --grids 20 --capital 1000 --range-pct 10
python3 -m defigrid backtest --days 40 --lower 2200 --upper 2700 --mode geometric --live-impact

# 5. 一次試多組格數 × 區間:前 70% 資料排名(樣本內),後 30% 驗證(樣本外)
python3 -m defigrid scan --days 90 --grids-list 5,10,20,40 --ranges 5,10,15,20
```
`scan` 會顯示:樣本內排名、各組在樣本外的報酬、樣本外賺錢與勝過持有幣的組合數,以及「排名相關性」
(樣本內排名對樣本外的延續程度)。**請以樣本外結果為主**:試越多組,越容易挑到剛好貼合過去行情的參數。

```bash
# 6. 滾動式驗證:每輪用過去 30 天挑出最佳參數,拿接下來 7 天實跑,再往後推 7 天,反覆到最新資料
python3 -m defigrid walk --days 120 --train-days 30 --test-days 7 --grids-list 5,10,20,40 --ranges 5,10,15,20
```
`walk` 比 `scan` 的單一切點更不靠運氣。它會列出每一輪選中的參數與樣本外結果,並把各輪連乘成整體報酬,
再跟兩個對照比較:同期持有幣,以及「整段固定用同一組參數」的中位數。
挑出來的參數如果沒有明顯勝過中位數,就代表「看過去挑參數」沒有用。
加 `--anchored` 時,樣本內會從資料開頭一路累積,而不是固定長度。
```bash
# 7. 用鏈上逐筆成交(最近 1~48 小時)檢查:小時 K 線 + 路徑猜測,低估了多少成交
python3 -m defigrid swaps --hours 24 --grids 40 --range-pct 2

# 8. Uniswap v3 集中流動性(LP)部位 vs 自己下 swap 的網格 vs 持有幣
python3 -m defigrid lp --days 30 --range-pct 10 --grids 20
```
`swaps` 會用同一批成交跑兩次:一次照真實成交順序,一次先彙整成小時 K 線再猜路徑,比較兩者的成交與循環次數。
`lp` 會估算手續費收入與無常損失,並和同區間的網格比較;`lp` 的 `--token` 必須是池子的 token0(預設 WETH)。

### 即時模擬盤(paper trading)
用鏈上**每一筆真實成交**持續推進網格,記錄「如果真的下單會發生什麼」。不碰錢包、不送交易。
```bash
# 建立並開始跑(以目前價格 ±20%、20 格、本金 1000;每 15 秒檢查一次,Ctrl+C 停止)
python3 -m defigrid paper run --name eth20 --range-pct 20 --grids 20 --capital 1000

# 之後接續(會從上次停下的區塊補抓,設定沿用建立時的)
python3 -m defigrid paper run --name eth20

# 查看狀態與最近成交(不連網);列出所有模擬盤
python3 -m defigrid paper status --name eth20
python3 -m defigrid paper list

# 放到背景長時間跑(-u 讓輸出即時寫進記錄檔)
mkdir -p paper && nohup python3 -u -m defigrid paper run --name eth20 > paper/eth20.log 2>&1 &
```
- 狀態存在 `paper/<名稱>.json`,成交記錄在 `paper/<名稱>.trades.jsonl`(`paper/` 已被 .gitignore 排除)。
- 電腦睡眠或關機後再執行會自動補抓;停機超過 48 小時只補最近 48 小時,中間那段會漏算並在狀態中提示。
- 想換設定就用新的 `--name` 另開一個,可以同時跑好幾組比較。
- 也可以用 `--once` 只更新一次,搭配排程定時執行。

常用參數:`--fee-bps`(池子手續費,預設 5)、`--slippage-bps`(預設 2)、`--gas-usd`(每筆 gas,預設 0.03)、
`--path`(一根 K 線內的價格路徑假設,用來看結果對這個假設敏感不敏感)。

## 模擬的假設與限制(結果要看這些前提)
- **K 線內的價格路徑是猜的**:預設陽線 開→低→高→收、陰線 開→高→低→收。真實盤中會來回震盪、穿越更多次,
  所以模擬傾向**低估**成交次數。可用 `--path low-first / high-first` 比較,差距大代表結果不穩。
  `swaps` 指令可以實測低估程度:格距 0.6% 時幾乎沒差,格距 0.1% 時小時 K 線只抓到約 73% 的循環。
- 以限價單方式成交在格線價,**沒有**模擬交易被搶先(MEV)、交易失敗或延遲。
- 價格衝擊只用「目前價格區間」內的流動性估算,只適合相對池子深度很小的交易。
- gas 用美元計、另外記帳(視為另付 ETH),回測期間固定一個值。`gas` 指令會實測 L1 資料費,目前遠小於 1 美分,可以忽略。
- 歷史價格來自第三方彙整,偶爾有異常價格,程式會對單小時振幅超過 15% 的 K 線發出警告;最新一根尚未收完的 K 線不納入。
- 最大回撤只看每根 K 線收盤時的資產,盤中更深的回撤不會反映。
- 報酬有兩種:「持幣以收盤價計」與「期末全部賣回 USDC 的出場後報酬」;`walk` 每輪都用出場後報酬(換輪要先出場)。
- `lp` 的手續費收入是**粗估**:池子流動性固定用現在鏈上的值,成交量依 K 線高低點和區間的重疊比例分攤;沒有考慮 JIT 流動性與 MEV。
- 公開 API 每分鐘約 30 次請求;連續下載太多會被限流(HTTP 429),等一分鐘再試即可。K 線會快取一小時。
- 模擬盤以「每筆鏈上成交後的價格」判斷穿越格線,包含夾擊交易等瞬間價格,成交次數偏樂觀;沒有模擬自己下單的延遲與失敗。
- 回測只代表過去那一段行情;網格在單邊行情會持有越跌越多的幣或賣光,績效與行情走勢高度相關。

## 目錄結構
```
defi-grid/
├── defigrid/
│   ├── rpc.py       # 唯讀 JSON-RPC(錯誤訊息不外洩含金鑰的網址)
│   ├── uniswap.py   # 池子狀態、ABI 解碼、價格與價格衝擊
│   ├── data.py      # 歷史 K 線(取得、驗證、快取)
│   ├── sim.py       # 網格模擬引擎與損益估算(純計算)
│   ├── scan.py      # 參數掃描、樣本內 / 樣本外驗證、排名相關性、滾動式驗證
│   ├── swaps.py     # 鏈上 Swap 事件(逐筆成交價格)
│   ├── lp.py        # Uniswap v3 集中流動性部位模擬
│   ├── paper.py     # 即時模擬盤(狀態保存、斷點接續)
│   └── cli.py       # 命令列
├── tests/           # 單元測試(75 項,不需連網)
├── paper/           # 模擬盤狀態與成交記錄(已被 .gitignore 排除)
├── cache/           # K 線快取(已被 .gitignore 排除)
└── doc/             # 文件
```

## 路線圖
1. ✅ 唯讀:鏈上價格、池子、gas
2. ✅ 模擬:歷史回放,含成本;參數掃描、樣本外 / 滾動式驗證、逐筆成交校驗、LP 比較、即時模擬盤
3. ⬜ 測試網:用假幣走完整下單、成交、對帳流程(需要測試用錢包,不放真資產)
4. ⬜ 極小金額主網:專用熱錢包、總開關、每日虧損上限
詳見 [doc/plan.md](doc/plan.md)。

## doc/ 文件索引
- [工作日誌](doc/worklog.md)
- [待辦清單](doc/todo.md)
- [計畫與設計](doc/plan.md)
