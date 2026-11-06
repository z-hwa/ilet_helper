# 工作紀錄（WORKLOG）

每次改動都記在這裡（新的在上面）。執行期的使用痕跡另外在：
- `logs/bot.log`：所有程式輸出（5MB 輪替，保留 10 份）
- 資料庫 `app_events`：指令使用、錯誤（含 traceback）、被忽略的訊息、逾時
- 資料庫 `api_payloads`：每次 Gemini 呼叫的提示詞與回應原文（預設保存 30 天）
- 查詢：`python logview.py events|errors|user <id>|calls|call <id>`

## 2026-11-04
- 新增 log 基礎建設（檔案 log、`app_events`、`api_payloads`、`api_calls.error_text`、`logview.py`），兩隻 bot 的指令/錯誤/逾時都會記錄。
- docs6 #8：德語口說 / 練習的 stop、skip 正規化（Stop.、/stop、Stopp、停止、aufhören…都算）；打字被忽略時第一次會提醒；每個 session 加鎖，語音與 stop 同時到不再搶狀態、不再出現 FileNotFoundError。
- 移除 /de_ask 的「AI 老師偶爾會講錯」免責聲明。
- docs6 #4：例句去重（比對所有出過的句子、相似度 ≥0.82 重出最多 3 次、沒指定情境時從 30 種情境隨機抽）。
- docs6 #5：例句（句子＋發音＋單字＋文法）盡量合成一則訊息，太長才分兩則。
- docs6 #6：德語評級顯示「B1+ (3.76)」（變化行與 /de_level）。
- docs6 #7：翻譯題顯示要翻的中文句子，提示另行顯示；出題規則改成 prompt = 中文原句。
- docs7：雅思 /speaking 沒指定主題時隨機抽 Part 1/2 話題，並把以前出過的題目給 Gemini 避開；仍重複就重出最多 3 次，並記錄 duplicate_speaking_set 事件。
- 待辦：研究 Gemini 為何抓不到有瑕疵的填空題（t_research.py 已刪，需要重跑）；docs6 #1 例句 JSON 解析錯誤已由 lenient 解析處理，持續觀察 logview.py errors。
- 2026-11-04 德語例句改回兩則（句子＋發音／解析），因為 Discord 附件固定顯示在文字最下面；clean() 會移除中文字之間多餘空格。
- 2026-11-04 例句下方加按鈕：🐢 0.5 倍速整句、🔊 各重點單字（只有按的人看得到的語音）。
- 2026-11-04 例句第一則附兩個語音檔（原速、0.5 倍速），按鈕只留單字發音。
- 2026-11-04 初始化 git；自我檢查：IELTS 也加上 per-user 鎖（stop 與語音同時到不再搶狀態）；新增 tests/（21 個邏輯測試＋德語 stop 流程測試）；新增 CLAUDE.md。
- 2026-11-04 新增口說「對話追問」：dialogue.py（對話樹、關鍵字比對）、asr.py（本地 faster-whisper）、bot.py 的 start_dialogue / advance_dialogue；/speaking 選單新增選項。實測比對品質後把 prompt 改成開放式問題＋內容導向分支，門檻 MIN_SCORE=1/MIN_MARGIN=1。測試：tests/test_logic.py（27 個）、tests/live_dialogue.py、tests/measure_matching.py。
- 2026-11-04 對話追問比對門檻改 MIN_SCORE=2（117 句擬真回答 0 句接錯、61% 走分支）；measure_matching.py 支援 --more 累積樣本。
- 2026-11-04 新增 /listen_test 聽力模擬測驗（雅思格式 Section 1~4、完整 40 題）：listening_test.py、bot.py 的 ListenTestSession / run_listen_section / grade_listen_section / handle_listen_answer；tts.synthesize 支援 .ogg（分塊寫入，避免 libsndfile segfault）；週報把 listen_test 算進聽力練習場數。實測 4 個 Section 各 10/10 題有效。
- 2026-11-04 /listen 與 /listen_test 合併成單一 /listen 指令，用 mode 選「快速練習」或「雅思模擬」（section 只在雅思模擬使用）；已不再有 /listen_test。
- 2026-11-04 語音支援多個說話者：tts.ACCENTS 增加 C、D 兩個聲音（美英各一女一男）；雅思模擬 Section 3 允許 2~4 人，快速練習允許 3 人；parse_section 只收 A~D。
- 2026-11-04 雅思模擬加入人物說明：Gemini 給 speakers（代號=名字與身分），題目卷與原文顯示對照，錄音開頭由旁白（tts 聲音 N）念 Section 與說話者介紹。
- 2026-11-05 docs7 #1 德語寶寶多聲音：下載 Piper 的 Kerstin/Ramona（女）與 Karlsson（男）放進 models/de；german.py 的 voice_files/voice_for/voice_label，synth 加 voice 參數；例句依 id 固定聲音、口說每場隨機老師；新增 tests/test_voices.py。
- 2026-11-05 Gemini 503 備援：ask_gemini 主模型回 503 時立刻改用 GEMINI_FALLBACK_MODEL（預設 gemini-3.5-flash；gemini-2.5-flash 已對新用戶下架 404、gemini-3.8-flash 實測也 503），記 api_events kind=fallback，api_calls.model 記實際用的模型；新增 tests/test_fallback.py。
- 2026-11-05 /de_sentence 加時態模式 tense：隨機（預設）／現在式／過去式／現在完成式／未來式／不限；german_logic 的 TENSES、pick_tense、tense_ok（Futur 檢查 werden、Perfekt 檢查 haben/sein），make_sentence(tense=) 沒用到時態就重出（最多 3 次，記 wrong_tense 事件）；標題顯示時態，記 sentence_tense 事件。每日例句不變。真 Gemini 測試當下兩個模型都 503，沒測成。
- 2026-11-06 德語寶寶新增 `/de_gossip` 閒聊模式：新 gossip.py（prompt/解析/排版/報告）、german.py 的 GossipSession/handle_gossip/finish_gossip/de_gossip、資料表 de_gossip_sessions/de_gossip_turns；每回合即時點評文法（聽音檔），結算給評分、建議、遮擋的逐字稿與英文翻譯；新增 tests/test_gossip.py、tests/flow_gossip.py。
- 2026-11-06 準備公開到 GitHub：掃描 git 歷史（token/API key/Discord ID 皆無外洩；發現 docs6 貼有真人 Discord 暱稱、commit 作者信箱是 gmail），改用乾淨的新歷史（分支 main；舊歷史改名 local-history-private，只留本機）；ilets_baby/、deutsch_baby/ 筆記改為 gitignore；新增 README、LICENSE（MIT）、重寫 .env.example、docs/ARCHITECTURE.md、scripts/download_models.sh、scripts/check_secrets.py、.githooks/pre-commit、.github/workflows/tests.yml、docs/mockups/build.py 與 docs/images 示意圖；刪除用過即丟的 t_a9.py、t_b4.py；測試腳本不再寫死個人路徑。
