# تقييم صارم للمستودعات مفتوحة المصدر (2026-11-23)

**المنهج:** لا اعتماد على عدد النجوم أو مقالات المقارنة (كثير منها تسويقي). لكل مرشح: (1) نسخة سطحية من المستودع فُحصت ملفاتها **دون تشغيل أي كود منها** — الترخيص، آخر commit، الاختبارات، الـ CI؛ (2) بيانات PyPI الرسمية — آخر إصدار وتاريخه وعدد الإصدارات خلال 12 شهراً والترخيص؛ (3) فحص موجّه داخل الكود للادعاءات المهمة.

**ثلاثة ادعاءات من نتائج البحث صُحِّحت بالتحقق المباشر:**
1. "cryptofeed مؤرشف منذ 2026-07-07" ← **خطأ**: آخر commit بتاريخ 2026-11-18 وإصدار 3.0.5 على PyPI بالتاريخ نفسه.
2. "purgedcv في مرحلة ألفا" ← قديم: الإصدار 1.0.0 (2026-11-16) مصنّف Production/Stable.
3. نتيجة PBO "خاطئة" في اختبارنا الأول ← **خطؤنا**: المكتبة تتوقع المصفوفة بشكل `(عدد الإعدادات، عدد الفترات)`، كما في توثيقها.

## معايير التقييم

| المعيار | 2 | 1 | 0 |
|---|---|---|---|
| الترخيص | متساهل (MIT/BSD/Apache) | LGPL (استخدام كمكتبة) | GPL/AGPL أو غير مفتوح (Commons Clause) أو بلا ترخيص |
| الصيانة | إصدار خلال 3 أشهر | خلال 12 شهراً | أقدم |
| الجودة | اختبارات + CI | أحدهما | لا شيء |
| الملاءمة | يحل حاجة الآن | حاجة لاحقة | لا حاجة |
| المخاطر | اعتماديات خفيفة، فريق | متوسطة | ثقيلة/فرد واحد بلا بديل |

## النتائج

| المستودع / الحزمة | الترخيص (مُتحقق) | آخر نشاط | الجودة | القرار | السبب |
|---|---|---|---|---|---|
| **purgedcv** 1.0.0 | MIT | commit 2026-11-22؛ 26 إصداراً/سنة | 55 ملف اختبار، 2 CI | ✅ **اعتماد الآن** (بحث) | يغطي منهجية [09 §6](../09-learning-validation.md#6-منهجية-التحقق) كاملة: CPCV، Purged K-Fold، Walk-Forward، تطهير/حجز، PSR، DSR، MinTRL، PBO؛ اعتماديات خفيفة. **نقطة الضعف: مطوّر واحد** ← إصدار مثبّت + 5 اختبارات تحقق مستقلة من معادلات الأوراق الأصلية ([`research/tests`](../../research/tests/test_validation_toolkit.py)) |
| **binance-sdk-derivatives-trading-usds-futures** 18.4.0 (Binance الرسمي) | MIT | 2026-11-17؛ 42 إصداراً/سنة | 113 ملف اختبار | ✅ مرجع الآن، **اعتماد للحارس** (المرحلة 4) | **حسم بنود التحقق المعلّقة** (انظر أدناه)؛ مسار كود مستقل تماماً عن النواة — مثالي للحارس المستقل |
| **nautilus_trader** 1.231.0 / 2.0 rc | LGPL-3.0 | commit اليوم؛ 17 إصداراً/سنة | 222 ملف اختبار + Rust، 12 CI | ⏳ اعتماد لاحق (المرحلة 3) | [SPIKE-001](../spikes/SPIKE-001-trading-kernel.md): على أول 2.x مستقر |
| **skfolio** 1.14.4 | BSD-3 | 2026-11-21؛ 103 إصدارات/سنة | 247 ملف اختبار، 6 CI | ⏳ لاحقاً (بناء المحافظ) | جيد جداً، لكن اعتماديات ثقيلة (cvxpy، clarabel، plotly)؛ CPCV متوفر أصلاً في purgedcv |
| optuna 5.1 / lightgbm 4.7 / scikit-learn 1.9 / mapie 1.6 / shap 0.54 / mlflow 3.18 | MIT/BSD/Apache | نشطة | ناضجة | ⏳ لاحقاً (المرحلة 5) | ضمن التصميم؛ لا تُضاف قبل الحاجة |
| clickhouse-connect 1.13 / polars 2.1 / duckdb 2.0 | Apache/MIT | نشطة | ناضجة | ⏳ لاحقاً (المرحلة 1–2) | استيعاب ClickHouse وتوسيع البحث |
| ccxt 4.5 | MIT | نشط جداً | ناضج | ⏳ لاحقاً (المرحلة 7) | بيانات عبر المنصات |
| **freqtrade** 2026.10 | **GPL-3.0** | نشط | 143 ملف اختبار، 9 CI | 📚 مرجع فقط | ناضج ومجرّب، لكن التنفيذ في الاختبار الرجعي قائم على الشموع (لا دفتر أوامر)، والتكاليف مسؤولية المستخدم، وGPL؛ نأخذ أفكاراً (حمايات الحساب، أنماط FreqAI) لا كوداً |
| freqtrade-strategies | GPL-3.0 | 2026-09 | بلا اختبارات | 📚 مصدر أفكار | كل فكرة تمر بالتسجيل المسبق والاختبار عندنا |
| jesse 3.7 | MIT | نشط جداً | 114 ملف اختبار | 📚 مرجع | تصميم بحثي نظيف؛ قائم على الشموع، وشروط التداول الحي غير واضحة |
| quantstats 0.0.86 | Apache-2.0 | 2026-11 | 9 ملفات اختبار | 📚 مرجع | اعتماديات ثقيلة (منها yfinance التي تحتاج إنترنت)؛ مقاييسنا مكتوبة أصلاً |
| binance-public-data (سكربتات رسمية) | **بلا ملف ترخيص** | 2026-10 | — | 📚 مرجع | عندنا أداة أقوى (تحقق SHA256 + كتابة ذرية) |
| awesome-systematic-trading | **بلا ملف ترخيص** | 2026-11 | — | 📚 فهرس أفكار | روابط فقط، لا نسخ محتوى |
| vectorbt 1.2.1 | Apache-2.0 **+ Commons Clause** | 2026-11 | 16 ملف اختبار | ❌ رفض | ليس مفتوح المصدر بالمعنى الكامل (يمنع بيع ما يعتمد عليه أساساً)، ومطوّر واحد |
| backtesting.py 0.6.6 | **AGPL-3.0** | 2026-07 | ملف اختبار واحد | ❌ رفض | ترخيص متشدد ونطاق محدود |
| cryptofeed 3.0.5 | **AGPL-3.0** | 2026-11-18 | 31 ملف اختبار | ❌ رفض | الترخيص؛ والمسجّل الخاص بنا يغطي حاجتنا مع ضمانات أقوى |
| hummingbot | Apache-2.0 | نشط | 561 ملف اختبار | ❌ خارج النطاق | مصمم لصناعة السوق والمراجحة |

## ما حسمه كود Binance الرسمي (كان معلقا)

**1. توجيه WebSocket — كامل ومؤكد** (`websocket_streams/streams/public_api.py` و`market_api.py`):

| المسار | البث |
|---|---|
| `/public` | الدفاتر التفاضلية والجزئية، bookTicker (لرمز ولكل السوق)، ودفتر RPI التفاضلي |
| `/market` | aggTrade، `forceOrder` (لرمز ولكل السوق)، `miniTicker` و`ticker` (لرمز ولكل السوق)، `contractInfo`، سعر العلامة، الشموع (ومنها المستمرة)، المؤشرات المركّبة، جلسات التداول |
| `/private` | بيانات الحساب |

← جدول التوجيه الافتراضي في المسجّل **مطابق بالكامل**، بما فيه البنود الثلاثة التي كانت غير مؤكدة.

**2. Algo Order API** (`rest_api/api/trade_api.py`): `POST /fapi/v1/algoOrder` بمعاملات: `algoType` (`CONDITIONAL`)، `symbol`، `side`، `type` (منها `STOP_MARKET`)، `positionSide`، `timeInForce`، `quantity`، `price`، **`triggerPrice`** (وليس `stopPrice`)، `workingType` (`MARK_PRICE`/`CONTRACT_PRICE`)، `priceMatch`، `closePosition`، `priceProtect`، `reduceOnly`، `activatePrice`، `callbackRate`، **`clientAlgoId`**، `newOrderRespType`، `selfTradePreventionMode`، `goodTillDate`، `recvWindow`.

**3. تغيير قادم اكتُشف — "CM migration":** توثيق الكود الرسمي يذكر أن بث السوق الكامل (`!bookTicker`، `!forceOrder@arr`، `!miniTicker@arr`، `!ticker@arr`) سيحمل عقود USD-M وCOIN-M معاً، مع حقل جديد `st` (`1` = USD-M، `2` = COIN-M)، ويُضاف `st` إلى رسائل aggTrade. **الأثر عندنا:** المسجّل يسجّل الرسائل خاماً فلا يتأثر، لكن أي تحليل لبث السوق الكامل **يجب أن يرشّح `st = 1`** — مُدرج في المتطلبات.

## المصادر

- [Freqtrade vs Hummingbot vs Jesse (2026)](https://crypto-algo-dev.com/trading-bot-frameworks-comparison/)
- [20+ Algo Trading Frameworks Reviewed](https://autotradelab.com/blog/nautilus-vs-vectorbt-vs-freqtrade-20-python-quant-trading-frameworks-compared)
- [vectorbt README (license)](https://raw.githubusercontent.com/polakowo/vectorbt/master/README.md)
- [purgedcv on PyPI](https://pypi.org/project/purgedcv/)
- [AutoQuant — execution realism in open-source frameworks](https://arxiv.org/pdf/2512.22476)
- [CCXT vs Cryptofeed](https://docs.ccxt.com/docs/comparisons/ccxt-vs-cryptofeed)
- [cryptofeed repository](https://github.com/bmoscon/cryptofeed)
- [awesome-systematic-trading](https://github.com:443/paperswithbacktest/awesome-systematic-trading)
- المستودعات المفحوصة مباشرة: nautechsystems/nautilus_trader، freqtrade/freqtrade، freqtrade/freqtrade-strategies، hummingbot/hummingbot، jesse-ai/jesse، polakowo/vectorbt، kernc/backtesting.py، skfolio/skfolio، ranaroussi/quantstats، bmoscon/cryptofeed، binance/binance-connector-python، binance/binance-public-data، paperswithbacktest/awesome-systematic-trading، optuna/optuna، eslazarev/purged-cross-validation.
