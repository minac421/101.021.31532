# مراجعة الأدلة المنشورة — ما الذي "جُرِّب ونجح" فعلاً؟ (2026-11-23)

**الغرض:** اختيار الفرضيات التالية من أفكار لها دليل منشور، بدل التخمين. **المبدأ:** أي دليل خارجي — مهما كان مصدره — هو **فرضية** عندنا حتى يجتاز خط التحقق نفسه (تسجيل مسبق، تكاليف واقعية، بيانات لم تُستخدم).

## الخلاصة

| الفكرة | قوة الدليل | أهم المصادر | ملاحظات | الأولوية عندنا |
|---|---|---|---|---|
| **الزخم الزمني / تتبّع الاتجاه** (أسابيع) | **الأقوى** | Han, Kang & Ryu: الدليل على الزخم الزمني قوي بعد التكاليف، والزخم المقطعي ضعيف، والأثر مركّز في الأسواق الصاعدة؛ Fieberg (JFQA): عامل الاتجاه المقطعي CTREND يصمد أمام التكاليف حتى 4 أسابيع | مراجعة 2026 (Pang & Wang) تُظهر أن بعض النتائج حساسة لقواعد التنفيذ والكون؛ ادعاءات Sharpe المرتفعة جداً (2.41) من مسودات بحثية تُعامل بحذر | **H-002 الآن** |
| **الزخم المقطعي** (شراء الأقوى/بيع الأضعف) | متوسطة | Liu, Tsyvinski & Wu (Journal of Finance 2022): عامل الزخم ضمن نموذج ثلاثي العوامل | التعرّض للعامل عابر؛ أدلة أحدث مختلطة بعد التكاليف | لاحقاً (H-003) |
| **المراجحة بالتمويل/الأساس (Carry)** | قوية تاريخياً | Schmeling, Schrimpf & Todorov (BIS WP 1087): الفارق قد يتجاوز 40% سنوياً أحياناً، مدفوع بطلب المتداولين الصغار على الرافعة | تتطلب السوق الفوري مع العقود (خارج نطاقنا الحالي)؛ مسح 2025: الربحية **انضغطت بشدة منذ 2024**؛ مخاطر انهيار | مؤجلة (تحتاج قرار نطاق) |
| **التمويل/العقود المفتوحة كإشارة اتجاه** | ضعيفة | لا دراسة صارمة للتنبؤ خارج العينة؛ دراسة MDPI 2026: 40% فقط من أفضل فرص مراجحة التمويل ربحت بعد التكاليف | ادعاءات الممارسين (منصات وإعلام) غير مختبرة | ميزة مساعدة، لا إشارة رئيسية |
| **الارتداد بعد شلالات التصفيات** | ضعيفة/مختلطة | arXiv 2607.27070: لا مقياس أنذر بكل الشلالات السبعة؛ تحليل K33: أداء BTC يميل للضعف 30 يوماً بعد ذروات التصفية | بعض الذروات قيعان محلية، وأغلبها لا | منخفضة (S6 تبقى صغيرة الحجم) |
| **استراتيجيات GitHub/البوتات الجاهزة** | ضعيفة غالباً | مصادر Freqtrade: اختبارات الشموع تبالغ، والرسوم صفر افتراضياً؛ arXiv 2609.34510: ترتيب الاستراتيجيات يتغير جوهرياً من الاختبار الرجعي إلى الورقي | المنهج المرحلي (رجعي ← ورقي ← حي) هو التوصية — وهو منهجنا | مصدر أفكار فقط |
| **إشارات دفتر الأوامر قصيرة المدى** | قوية لكن لآفاق ثوانٍ | (أدبيات تدفق الأوامر) | ميدان HFT — التكاليف تقتلها عندنا (كما أكدت H-001) | للتوقيت فقط، بعد تراكم تسجيلاتنا |

## ما يعنيه هذا للتصميم

1. **الأفق الأطول أفضل لنا:** الأدلة المنشورة ونتيجة H-001 يتفقان — الميزات الموثقة تعيش على مدى أيام إلى أسابيع، والتكاليف تقتل الآفاق القصيرة. مقترح (يحتاج قرارك): عائلة "اتجاه" بأفق احتفاظ حتى ~4 أسابيع ورافعة منخفضة — **دون أي تغيير آلي في سياسة المخاطر**.
2. **"الحيتان" ودفتر الأوامر:** تبقى أدوات **توقيت وتجنب مخاطر** فوق إشارة اتجاه مثبتة، لا مصدر ربح مستقلاً حتى يثبت العكس على تسجيلاتنا.
3. **نتوقع تدهوراً بعد النشر:** الأفكار المنشورة تضعف عادة بعد نشرها؛ لذلك معايير القبول عندنا أشد، والتحقق يتم على فترات لم تستخدمها الأوراق المذكورة.

## المصادر

- Han, Kang & Ryu — *Time Series and Cross Sectional Momentum in the Cryptocurrency Market* — https://acfr.aut.ac.nz/__data/assets/pdf_file/0009/918729/Time_Series_and_Cross_Sectional_Momentum_in_the_Cryptocurrency_Market_with_IA.pdf
- Fieberg et al. — crypto trend factor (JFQA) — https://www.cambridge.org/core/services/aop-cambridge-core/content/view/S0022109024000747
- Pang & Wang — *The Cryptocurrency Trend Factor Revisited* — https://arxiv.org/pdf/2611.01951
- Bui & Nguyen — *Systematic Trend-Following with Adaptive Portfolio Construction* — https://arxiv.org/pdf/2602.11708
- Liu, Tsyvinski & Wu — *Common Risk Factors in Cryptocurrency* — https://www.nber.org/papers/w25882.pdf
- *Cryptocurrency as an Investable Asset Class: Coming of Age* — https://arxiv.org/pdf/2510.14435
- Schmeling, Schrimpf & Todorov — *Crypto Carry* (BIS WP 1087) — https://www.bis.org/publ/work1087.pdf
- He, Manela, Ross & von Wachter — perpetual futures pricing — https://arxiv.org/pdf/2212.06888v5
- MDPI Mathematics 14(2):346 — funding-rate arbitrage after costs — https://www.mdpi.com/2227-7390/14/2/346
- *Where does the criticality live?* — liquidation cascades — https://arxiv.org/pdf/2607.27070
- The Block — K33 on returns after liquidation spikes — https://www.theblock.co/post/372115/whats-next-after-cryptos-fourth-largest-long-liquidation-event-of-2025
- Freqtrade backtesting traps — https://brookmiles.github.io/freqtrade-stuff/2021/04/12/backtesting-traps
- *Can AI Make Money in Crypto? Measuring the Gap from Backtests to Real Markets* — https://arxiv.org/pdf/2609.34510
