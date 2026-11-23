# wt-collector — مسجّل بيانات السوق (للقراءة فقط)

أول مكوّن تشغيلي في النظام، ويُشغَّل **فوراً** لأن تاريخ دفاتر الأوامر لا يتوفر من Binance، وتاريخ OI ونسب Long/Short عبر REST لا يتجاوز 30 يوماً ([03 §6](../../docs/03-data-sources.md)).

> **آمن بالتصميم:** يستخدم نقاط البيانات العامة فقط — **بلا مفاتيح API، ولا يوجد فيه أي مسار يرسل أوامر**. تشغيله لا يعرّض رأس المال لأي خطر.

## ماذا يسجّل

| النوع (`kind`) | المصدر | لماذا |
|---|---|---|
| `ws.aggTrade` | كل رموز الكون (`/market`) | التدفق العدواني، CVD، البصمات، الشموع |
| `ws.depth` + `rest.depth` | رموز التركيز: الفروقات 100ms + لقطات REST عند البدء وبعد أي فجوة ودورياً | يمكن **إعادة بناء الدفتر بدقة دون اتصال** (مُختبر آلياً) |
| `ws.bookTicker` | رموز التركيز (`/public`) | السبريد والتنفيذ |
| `ws.markPrice` | `!markPrice@arr@1s` | التمويل، العلاوة/الأساس |
| `ws.forceOrder` | `!forceOrder@arr` | التصفيات (**عيّنة**: أكبر تصفية لكل رمز في الثانية) |
| `ws.miniTicker`، `ws.contractInfo` | مصفوفات السوق | الإحصاءات وتغييرات العقود |
| `rest.openInterest` | كل الرموز كل 5 دقائق | OI لحظي |
| `rest.futures_data.*` | التركيز كل 5 دقائق، البقية كل ساعة | OI التاريخي، نسب L/S، حجم المشترين/البائعين العدوانيين |
| `rest.exchangeInfo`، `rest.ticker24hr`، `rest.fundingInfo`، `rest.time` | دوري | الكون، الفلاتر، فترات التمويل، انحراف الساعة |
| `control`، `quality.summary` | محلي | أحداث الاتصال والتدوير، وملخص الجودة كل دقيقة |

كل سطر = غلاف حول **البايتات الخام كما وصلت**:
`{"recv_ns": …, "mono_ns": …, "origin": "…", "payload": <JSON الخام>}`
في ملفات `{root}/binance-um/{kind}/date=YYYY-MM-DD/hour=HH/{instance}-{part}.jsonl.zst`.

## الضمانات المُختبرة

- **تدوير بلا فجوات** قبل حد الـ 24 ساعة: اتصالان متداخلان لثوانٍ، وإزالة التكرار بمعرفات التسلسل.
- **مزامنة الدفتر** وفق الإجراء الرسمي + نافذة إعادة ترتيب؛ أي فجوة ← لقطة جديدة تلقائياً ومسجّلة.
- **لا يُحجب قارئ WebSocket أبداً**: الكتابة في خيط مستقل؛ امتلاء الطابور = إسقاط **معدود** وتنبيه، لا تجميد.
- **قابلية القراءة بعد الانهيار**: كل دفعة تُغلق إطار zstd؛ أقصى فقد ≈ ثانيتان.
- **انضباط حدود المعدل**: نصف ميزانية وزن الـ IP فقط (الباقي للتداول لاحقاً)، توقف كامل عند 429/418، ميزانية منفصلة لـ `/futures/data/*`.
- **التوجيه الجديد**: `/public` للدفاتر، `/market` لبقية البيانات — يرفض الإقلاع إن لم يكن لنوع بث مسار مُعرَّف.

## التشغيل

```bash
uv sync                                                     # من جذر المستودع
cp config/collector.example.yaml config/collector.local.yaml   # ملف محلي غير متتبَّع
uv run wt-collector check-config --config config/collector.local.yaml
uv run wt-collector run --config config/collector.local.yaml
```

أو بالحاويات (المسجّل + Prometheus بتنبيهات صحة البيانات):

```bash
docker compose -f infra/docker-compose.yml up -d --build
```

أوامر مساعدة:

```bash
uv run wt-collector plan --config config/collector.local.yaml --exchange-info exchangeInfo.json --tickers ticker24hr.json
uv run wt-collector read data/raw/binance-um/ws.depth/date=2026-11-23/hour=14/<file>.jsonl.zst --limit 5
```

## المتطلبات على الخادم

- **مزامنة الوقت**: `chrony` مفعّل (المسجّل يقيس الانحراف عن ساعة المنصة وينبّه فوق 250ms).
- **القرص**: ابدأ بـ 1–2 TB (التقديرات في [04 §4](../../docs/04-databases.md))، مع نسخ الأرشيف الخام إلى تخزين سحابي.
- **الموقع**: منطقة منخفضة الكمون نحو Binance وولاية قضائية تسمح باستخدام المنصة ([ADR-012](../../docs/12-adr-log.md)).
- **الشبكة**: الوصول إلى `fapi.binance.com` و`fstream.binance.com`؛ منفذ المقاييس 9108 داخلي فقط.

## المقاييس الأهم (Prometheus على :9108)

`wt_ws_messages_total{kind}`، `wt_stream_lag_ms{kind}`، `wt_last_message_age_seconds{kind}`، `wt_book_live{symbol}`، `wt_book_snapshots_total`، `wt_agg_trade_missing_ids_total{symbol}`، `wt_recorder_dropped`، `wt_recorder_queue_depth`، `wt_rest_used_weight_1m`، `wt_rest_blocked_seconds`، `wt_clock_offset_ms`، `wt_disk_free_bytes`. قواعد التنبيه في [`infra/prometheus/rules/data-health.yml`](../../infra/prometheus/rules/data-health.yml).

## ما لم يُتحقق منه بعد

لم يُشغَّل على Binance الحقيقية من بيئة التطوير (الوصول الشبكي إلى Binance محجوب فيها)؛ كل الاختبارات تمت مقابل منصة مزيفة تحاكي البروتوكول والتوجيه. أول تشغيل حي على خادمك هو اختبار القبول: راقب `wt_book_live` و`wt_ws_connection_events_total` و`quality.summary` خلال الساعة الأولى، وطابق جدول التوجيه في `exchange.routes` مع الوثائق الرسمية (خاصة `forceOrder` و`miniTicker` و`contractInfo`).
