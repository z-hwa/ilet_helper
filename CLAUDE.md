# CLAUDE.md — 專案設計與工作守則

兩隻 Discord bot 共用一個程序、一個 SQLite 資料庫、一套 Gemini 用量防護：
- **雅斯寶寶 / ILETS baby**（`bot.py`）：雅思（IELTS）練習——單字、閱讀、聽力、口說，四項 band 評級。
- **德語寶寶 / Deutsch_baby**（`german.py` + `german_logic.py`）：德語每日例句、文法練習、口說、AI 問答，CEFR 評級。

使用者（專案擁有者）用繁體中文溝通；所有面向使用者的文字用繁體中文。使用者的需求紀錄在本機的 `ilets_baby/docs*.md`、`deutsch_baby/docs*.md`（他們在 Discord 使用後回報的問題，一份一份處理）。這兩個資料夾**已被 `.gitignore` 擋住、不進 git**（內容可能貼有 Discord 對話與暱稱），不要把它們加回去，也不要把裡面的人名、ID 抄進程式、文件或測試。

## 環境與指令
- WSL（Ubuntu20.04），專案在 `/root/Document/ilet_helper`，Python：`/root/miniconda3/envs/ielts_bot/bin/python`（沒有 `python` 在 PATH，要寫完整路徑）。
- 從 Windows 端操作用 `wsl -d Ubuntu20.04 bash -c '…'`；含引號的長腳本請用 Write 工具寫成檔案再執行（heredoc 在巢狀引號下容易壞）。
- 啟動：`python run_all.py`（兩隻 bot 同進程；只有雅斯寶寶用 `python bot.py`）。
- **不要自己啟動/重啟 bot**（使用者明確要求：他們自己開）。多開會讓每個回覆重複。需要重啟時提醒使用者。`pkill -f run_all.py` 會殺到自己的 shell，別用。
- 測試（不連網）：`python -m unittest discover -s tests -v`；`tests/flow_german_stop.py` 是假 Discord 的流程測試（要設 `DB_FILE`、`LOG_DIR` 到暫存位置，**絕不可寫入正式資料庫**）。
- 靜態檢查：`python -m pyflakes *.py`。
- 需要真實 Gemini 測試時：複製 `ielts_learning.db` 到 `/tmp` 並設 `DB_FILE`；免費額度每分鐘有限，呼叫之間要 sleep；結束後刪掉暫時腳本。
- **Git**：每個段落的工作做完就 commit（commit 訊息結尾加 Co-Authored-By 行）。`.env`、`*.db`、`logs/`、`audio_cache/`、`records/`、`models/` 都在 `.gitignore`；提交前掃描有沒有 token：`python scripts/check_secrets.py`（pre-commit hook 會自動跑，用 `git config core.hooksPath .githooks` 啟用；`--history` 掃整個歷史）。
- **公開 repo 的分支**：`main` 是乾淨的公開歷史（單一 root commit，作者信箱是 noreply，不含使用者筆記）；`local-history-private` 是公開前的舊歷史（含使用者 Discord 暱稱與作者 gmail），**只留在本機，絕對不要 push**。公開前一律先跑 `python scripts/check_secrets.py --history`。

## 守則（使用者明確要求過的）
1. **保留使用痕跡**：新功能要用 `log_event` / `log_error` 記錄；Gemini 提示詞與回應存 `api_payloads`。問題回報要能從 log 查到。每次改動追加到 `WORKLOG.md`。查詢：`python logview.py events|errors|user <id>|calls|call <id>`。
2. 不要在使用者面前重複講為什麼不能做某事（例：BBC 不收錄）；直接給替代方案。
3. 不要加免責聲明式的頁尾（例：de_ask 的「AI 老師偶爾會講錯」）。要解決問題本身。
4. 拼字要算錯（單字、德語大小寫），但德語允許 ae/oe/ue/ss 代替 ä/ö/ü/ß。
5. 秘密（`.env`）不要印出、不要進 git。
6. 不確定就先測試再說，回報時誠實：沒測的就說沒測。
7. 語音附件在 Discord 一定顯示在文字最下面——想讓聲音緊接句子，就把「句子」單獨成一則訊息並附音檔，解析放下一則。

## 架構
```
run_all.py        同時啟動兩隻 bot（一隻登入失敗不影響另一隻）
bot.py            雅斯寶寶＋所有共用基礎（德語模組以 `import bot as core` 使用）
  資料庫/評級/記錄/ask_gemini/guard/chunk_text/accounts 閘門/各種 Session
german.py         德語寶寶：指令、Session、Piper 配音、每日例句排程、按鈕
german_logic.py   德語純邏輯：prompt、解析、驗證、判分、格式化（不碰 Discord）
gossip.py         德語閒聊（gossip）純邏輯：開場/回合/評分 prompt、解析、即時點評排版、報告（不碰 Discord）
speaking.py       雅思口說純邏輯＋共用 spoiler_blocks / pack_blocks
exercises.py      閱讀/聽力題 prompt、證據定位、PDF/改編 prompt
listening_test.py 聽力模擬測驗（Section 1~4）純邏輯：prompt、解析驗證、作答解析、判分、band 換算
reports.py        週報
skills.py         評級演算法（純計算）
usage.py          Gemini 用量防護與分析（純邏輯）
accounts.py       使用者註冊/封鎖/遷移
sources.py        真實文章來源（開放授權；trafilatura；遵守 robots.txt；不收 BBC）
dialogue.py       口說對話追問的純邏輯（對話樹 prompt/解析、關鍵字比對、輪次狀態）
asr.py            本地語音轉文字（faster-whisper）
tts.py            英文本地 TTS（Kokoro）；德文用 Piper（在 german.py 的 synth）
jsonutil.py       寬鬆 JSON 解析（Gemini 偶爾回壞 JSON）
logview.py        查 log/事件/Gemini 呼叫
tests/            單元測試與流程測試
scripts/          download_models.sh（下載語音模型）、check_secrets.py（上傳前秘密掃描）
docs/             ARCHITECTURE.md、README 用的圖片（docs/images）與產圖腳本（docs/mockups/build.py）
.githooks/        pre-commit：commit 前掃描秘密（git config core.hooksPath .githooks 啟用）
README.md         給 GitHub 讀者的說明（安裝、設定、指令、隱私）
```

### 身分與多使用者
- 所有資料表都有 `user_id`。身分用 `usage.current_user`（ContextVar）：在 `CommandTree.interaction_check`、`on_message`、背景逾時處理裡設定。背景任務沒有指令可帶身分，必須自己 `current_user.set(...)`。
- 雅斯寶寶要 `/register`（Modal 輸入各項基礎 band）；德語寶寶要 `/de_register`（選 CEFR）。封鎖名單兩邊共用（`accounts`）。
- 管理員 = `OWNER_ID`（`/users /block /unblock`、全域用量）。伺服器白名單 `ALLOWED_GUILD_IDS`，被加進其他伺服器會自動退出。

### Gemini 呼叫（`bot.ask_gemini`）
所有呼叫都走這裡：用量防護（全域/個人 RPM、每日上限、重複 prompt 攔截、斷路器）→ 主模型 503 時立刻改用備援模型 `GEMINI_FALLBACK_MODEL`（預設 gemini-3.5-flash，留空關閉；記 `api_events` kind=fallback）→ 暫時性 5xx 自動重試（4s、10s）→ 寫 `api_calls`（含 `error_text`）與 `api_payloads`。回傳 `GeminiText`（帶 `call_id`，供 `mark_call/mark_consumed` 統計題目使用效益）。預設模型 `GEMINI_MODEL`（flash-lite，免費額度）。德語用 `german.ask_parsed`：解析失敗自動重問一次。
- 會把壞 JSON 當成可恢復錯誤：`jsonutil.loads_lenient` + 重試；題目另外在 `parse_exercises` 驗證（cloze 的 `full_sentence` 必須等於「把答案填回空格」；reorder 的詞組必須剛好湊出答案；翻譯題 prompt = 中文原句），壞題直接丟，有效題少於 3 題才報錯。
- Flash-lite 偶爾拼錯德文、抓不到瑕疵題；我們的驗證只管結構，不管語言正確性。

### Session 與並發
- 雅斯寶寶：`bot.quizzes[(channel_id, user_id)]` → `QuizSession | ExerciseSession | SpeakingSession`；德語寶寶：`GermanBot.sessions` → `PracticeSession | SpeakSession`。
- discord.py 會讓 `on_message` 同時執行。**每個 (頻道, 使用者) 有一把鎖**（IELTS：`bot.locks`；德語：`session.lock`），拿到鎖後要重新確認 session 還是同一個（可能剛被 stop 結束），否則記 `late_message`。逾時 watchdog 也要拿同一把鎖。
- 逾時（預設 10 分鐘）自動結束並評分；啟動時 `recover_abandoned_sessions()` 把上次殘留的 active session 收掉。
- stop/skip 判斷一律用 `german.command_word()`（正規化；容許大小寫、標點、`/`、全形、stopp/停止/aufhören…）。德語口說只收語音，打字忽略，只在第一次提醒。

### 評級（`skills.py`）
每項技能 `(rating, rd)`：對錯題用 Elo 式 `update_from_items`；口說/寫作用 Gemini 給的 band 做卡爾曼式 `update_from_band`；久不練習 rd 回升；單次練習對單項最多動 0.4。至少答 3 題才計入評級。德語用數值刻度 A1=1.5…C2=6.5（`de_grammar`、`de_speaking`），顯示成「B1+ (3.76)」（`german_logic.cefr_score`）。`/de_level`、`/progress` 有曲線圖（matplotlib）。

### 德語功能重點
- 每日例句（排程 `daily_loop`，`GERMAN_DAILY_*`）、`/de_sentence`（`tense` 選項：隨機〔預設〕/現在式/過去式/現在完成式/未來式/不限；`gl.TENSES`、`pick_tense`、`tense_ok` 只粗略檢查 Futur 與 Perfekt 的助動詞，沒用到就重出）：`make_sentence` 會避開以前所有例句（相似度 ≥0.82 重出，最多 3 次；沒指定情境時從 `SCENARIOS` 隨機）。
- `send_sentence`：第一則 = 句子＋翻譯＋情境＋**兩個語音檔**（原速 `satz.wav`、0.5 倍速 `satz_0.5x.wav`）＋重點單字按鈕（`PronounceView`，按了回只有自己看到的單字語音）；第二則 = 單字與文法解析。
- `/de_practice`：依弱點文法出題（cloze/choose/reorder/translate），逐題用文字作答；`/de_speak`：Piper 念題目、使用者傳語音、Gemini 聽音檔評分；`/de_ask`：以使用者最近的例句/練習題為脈絡回答。
- **`/de_gossip`（閒聊）**：`rounds`（3~12，預設 6）＋`topic`（選填）。Lena（虛構人物的生活八卦）先用語音開場，使用者每回合傳**語音**（打字忽略、只提醒一次）；每回合 1 次 Gemini 呼叫（**聽音檔**，不用本機 ASR）一次回傳：逐字稿、英文翻譯、`status`（perfect/ok/errors/unclear）、錯誤清單（said/fix/why）、更自然的說法、發音備註、Lena 的回應（德文＋英文）。即時點評訊息＝逐字稿（遮擋）＋✅/👍/✏️ 與逐項修正；Lena 的回應是另一段（德文與翻譯遮擋，Piper 語音附在同一則）。`unclear`（沒聽清楚）不算回合、不佔次數，請他再說。回合數到自動結算，或 `stop` 提前（至少 2 回合才評分；逾時 10 分鐘同樣結算）。結算 1 次文字呼叫（輸入是逐回合紀錄，**發音分數只能依各回合備註估計**）→ 總評、四項評分、建議例句、逐字稿與英文翻譯（遮擋，每回合附使用者錄音）；`de_speaking` 評級以 σ=0.7 更新。一場 = 回合數＋2 次呼叫。每次傳語音會帶內部序號進 prompt，避免重傳被『重複請求』防護擋掉。DB：`de_gossip_sessions`、`de_gossip_turns`；翻譯語言常數在 `gossip.TRANSLATION`。測試：`tests/test_gossip.py`（純邏輯）、`tests/flow_gossip.py`（假 Discord＋罐頭回應＋真 Piper）。
- 語音模型 Piper（`GERMAN_VOICE_MODEL` 為預設聲音 Thorsten），`synth(texts, out_path, slow, scale, voice)`：`scale=2.0` ≈ 0.5 倍速。**多聲音**：`voice_files()` 自動收集預設模型同資料夾裡有 `.json` 的 `.onnx`（目前 Thorsten 男、Karlsson 男、Kerstin 女、Ramona 女；後三個是 low 品質、16kHz，要換更多聲音就把模型丟進 `models/de/`，`GERMAN_MULTI_VOICE=0` 可關）。例句用 `voice_for(sentence_id)` 固定一個聲音（原速、0.5 倍速、單字按鈕同一人，訊息裡標「🎙️ 朗讀」）；口說練習每場隨機一位老師、整場同一人。男女聲對照在 `VOICE_GENDER`。

### 雅思功能重點
- 單字：`/word_quiz`（互動拼字）、`/word_review`、`/search`；閱讀 `/reading`（AI 生成或真實文章改編，來源見 `sources.py`）、`/reading_pdf`；聽力 `/listen`（Kokoro 本地配音）；口說 `/speaking`（Part1/2/3，考官配音，使用者傳語音）。
- `/speaking` 沒指定主題時從 `PART1_TOPICS`/`PART2_THEMES` 隨機抽，並把歷史題目（`speaking_answers`）交給 Gemini 避開，重複就重出（最多 3 次）。
- **口說「對話追問」**（ 選「對話追問」， + ）：3 輪對話。開場時 Gemini 一次產出「對話樹」（開場題＋3 個內容方向各有關鍵字與第 2 輪追問＋每個方向 2 個第 3 輪子分支＋通用追問）。每輪使用者答完，**本機** faster-whisper（，預設 small.en，CPU，約 1~4 秒）轉成文字 →  用關鍵字比對走哪個分支 → 不用 API。沒命中 → 通用追問（不花 API）；沒命中且字數 ≥6 → Gemini 即時接話（每場最多 =1 次）。一場約 2 次 API（出樹＋最後評分，評分仍聽原音檔）。每輪訊息會顯示「本機聽到的」與「接話方式」（劇透遮罩）。步驟用 ，所以評分與紀錄沿用口說流程。
  - 關鍵字比對的品質靠 prompt：開場題要開放式（What/Which/How），分支按「聊的內容」分、不按喜歡/不喜歡（否定句無法用關鍵字分辨）；關鍵字要單字、含口語與相關名詞。 實測（13 棵樹×9 個擬真回答，共 117 句）：現行  0 句接錯、61% 走分支、其餘走通用追問； 時約 3% 接錯、84% 走分支。改 prompt 或門檻後要重跑它。
- 閱讀「安靜模式」：作答流程中盡量不洗頻（題目訊息會被刪/編輯）。
- **聽力 `/listen` 有兩種 `mode`**：`quick`（預設，原本的短錄音 5 題、逐題即時回饋）與 `test`（雅思模擬，`section` 選項只有此模式用，進入點 `start_listen_test`）。雅思模擬：Section 1（對話・表格填空）/2（獨白・選擇＋配對）/3（討論・選擇＋填空）/4（講座・筆記填空），每段 10 題，或「完整測驗」連做 4 段（40 題，換算 band）。每段 1 次 Gemini 呼叫出腳本＋題目（`listening_test.build_prompt/parse_section`），Kokoro 本機配成 `.ogg`（約 2.5~3 分鐘、1 MB 左右）。流程：先貼題目卷 → 讀題 `LISTEN_PREVIEW_SECONDS`（預設 30 秒）→ 貼錄音（Discord 無法限制只播一次，只在文字說明）→ 使用者一行一題打答案（`1 Brown`、`3A 4C`，同一行寫多題也行，題號必須連續；打同一題會覆蓋；✅ 反應代表收到）→ `done` 交卷（完整測驗進下一段，`stop` 直接結束）→ 全部結束後一次公布成績、逐題解說（錄音依據）與原文。10 分鐘沒回應自動交卷。
  - 驗證：blank 題的答案必須真的出現在腳本裡（`in_script`，容許逐字母拼字與逐位數字），否則丟掉；有效題 <8 重出一次（第二次 prompt 加一句避免被重複攔截）。判分：不分大小寫、容許標點/連字號、拼字要對、小數字與阿拉伯數字等價、電話逐位=連寫；`alternatives` 是 Gemini 給的其他可接受寫法。
  - 評級：每題一個 (得分, 難度) 丟給 `update_skill_items("listening", …)`，難度 = 聽力 rating + Section 偏移（−0.5/0/+0.5/+1.0）；未作答算錯，答 ≥3 題才計入。DB 沿用 `exercise_sessions`（kind=`listen_test`，一段一列）與 `exercise_answers`。
  - 注意：`tts.synthesize` 寫 `.ogg` 必須分塊寫（`soundfile` 一次寫 >~100 秒會 segfault），已處理，改動時別改回一次寫。目前沒有地圖題。說話者 A~D 對應四個不同聲音、N 是旁白（雅思模擬每段錄音開頭念「Section n. 情境，The speakers are …」；題目卷與原文也列出 A=名字與身分）（A、C 女聲，B、D 男聲；tts.ACCENTS），Section 3 允許 2~4 人，快速練習允許到 3 人。
  - 測試：`tests/test_listening_test.py`（純邏輯）、`tests/flow_listen_test.py`（假 Discord＋罐頭題目）、`tests/live_listen_test.py`（真 Gemini＋Kokoro）。

### 記錄系統
- `logs/bot.log`（輪替 5MB×10）；DB：`app_events`（指令、錯誤含 traceback、`ignored_text`、`late_message`、`timeout`、`duplicate_*`…）、`api_calls`、`api_payloads`（提示詞與回應原文，預設 30 天，`PAYLOAD_RETENTION_DAYS`）、`api_events`（用量防護事件）。啟動時 `prune_logs()`。
- 注意隱私：`api_payloads` 與 `app_events.detail` 會包含使用者輸入的內容（作答文字、語音轉錄、提問）；檔案 log 本身只記字數。這是使用者要求保留使用痕跡的結果。

## 已知限制 / 待辦
- Gemini（flash-lite）有時抓不到瑕疵題，/de_ask 對「題目本身有問題」的反應不佳——「為什麼 Gemini 解決不了」的實驗式研究還沒做完（變體：把填回答案的完整句子放進 context、開 thinking、較強模型）。
- 德語文法「結構」欄位 Gemini 有時用縮寫（`Skl. + V.`），不清楚。
- 沒有 ⏭️/🛑 按鈕（口說/練習只能打字 skip/stop）。
- 按鈕 view 6 小時後失效，bot 重啟後舊訊息的按鈕也失效。
- 真正的 Discord 互動（slash command、語音訊息）無法在這裡自動測；只能用假 Discord 物件，加上請使用者在 Discord 驗證。
