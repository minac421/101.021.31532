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

## ملحق: أفكار الأفق القصير (دقائق إلى يوم) — مراجعة صارمة ثانية

**السؤال:** هل توجد أدلة منشورة على صفقات تُفتح وتُغلق خلال ساعات وتغطي تكلفتنا (taker 0.05% + انزلاق 0.02% لكل جانب ≈ 14 نقطة أساس ذهاباً وإياباً)؟ القاعدة: لا يُعدّ دليلاً إلا بحث يمكن فتحه والتحقق منه؛ المدونات لا تُحتسب. [FT] = قُرئ النص الكامل.

| الفكرة | أقوى دليل | الحجم | الحكم |
|---|---|---|---|
| زخم نصف الساعة (أول نصف ساعة يتنبأ بآخرها) | Shen, Urquhart & Wang (2022) *Financial Review* 57(2) [FT]؛ Wen et al. (2022) *NAJEF* 62 | R² ≈ 1.4%؛ نقطة التعادل 3–10 نقاط للصفقة، و1–3 فقط في 2020 (يتلاشى) | ❌ ميت حتى بأوامر maker |
| مواسم اليوم/الأسبوع/ربع الساعة | Baur et al. (2019) *FRL* 31؛ Caporale & Plastun (2019) *FRL* 31؛ Wu & Pinsky (2026) *JRFM* 19(9)؛ Kim & Hansen (2026) [FT] | قوية في الحجم والتقلب لا في العائد القابل للتداول؛ الجزء المتوقع ~0.5 نقطة (1/20 من تكلفة taker) | ❌ |
| أوقات تسوية التمويل وقنصه | Hansen, Kim & Kimbrough (2022) *J. Financial Econometrics* [FT]؛ He, Manela, Ross & von Wachter، *Fundamentals of Perpetual Futures* [FT] | قنص التمويل ~1.5 نقطة قبل التكاليف ويخسر؛ مراجحة الفورية/الدائم (تحتاج ساقاً فورية) قوية تاريخياً لكن الفجوة تضيق ~22 نقطة مئوية سنوياً | ❌ لعقود USD-M وحدها |
| ارتداد بعد التصفية القسرية | لا دليل منشور مباشر؛ الأقرب: Miralles-Quirós & Miralles-Quirós (2022) *J. Applied Economics* 25(1) — ارتداد البيتكوين بعد صدمات الساعة السلبية بأفق 6–24 ساعة | غير معروف | ⚠️ آلية اقتصادية + أفق طويل يخفف التكاليف ← **[H-005](H-005-deleveraging-reversal.md)** |
| استمرار/ارتداد بعد حركات متطرفة | Caporale & Plastun (2020) *FMPM* 34(3)؛ Zaremba et al. (2021) *IRFA* 78 | الارتداد اليومي أثر عدم سيولة؛ العملات الكبيرة تُظهر زخماً | ⚠️ ضعيف ومتضارب |
| اختلال تدفق الأوامر عند افتتاح ربع الساعة ← عوائد 4–12 ساعة | Kim & Hansen (2026)، Binance USD-M، 6 عقود، 2021-01 → 2024-10 [FT، تحققنا منه مباشرة] | مكوّن التدفق 5–6 نقاط (بين الربيع الأول والثالث) ثابت عبر الآفاق؛ مكوّن إشارات السعر والحجم 9.8 و16.9 نقطة عند 8 و12 ساعة؛ الإشارة تحتاج الثواني الأولى من كل ربع ساعة (بيانات صفقات) | ⚠️ هامشي أمام 14 نقطة |
| اختلال دفتر الأوامر | Bieganowski & Ślepaczuk (2026) [FT] | أفق 3 ثوانٍ؛ عائد taker للبيتكوين 13% مقابل 83% للاحتفاظ؛ الكمون غير منمذج | ❌ عالي التردد ولا تاريخ دفتر عندنا |
| اتجاه على شموع 1–4 ساعات | Mroziewicz & Ślepaczuk (2026)؛ Bui & Nguyen (2026، غير محكّم ومضبوط بكثافة) | ≈ الشراء والاحتفاظ خارج العينة | ❌ ضعيف |

**الخلاصة:** ما تحت الساعة يتراوح بين 0.5 و10 نقاط قبل التكاليف — يتسق مع رفض [H-001](H-001-sweep-reclaim.md). الإشارات المشروطة بأفق 4–24 ساعة وحدها قد تغطي التكاليف، ولا شيء منها مُكرَّر باستقلال بعد 2021 صافياً من التكاليف. **التداول على مدار الساعة لا يعني صفقات كثيرة:** يعني مراقبة مستمرة والدخول فقط عند حدث نادر بعائد متوقع أكبر بوضوح من التكلفة.

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
- Shen, Urquhart & Wang (2022), *Bitcoin intraday time series momentum*, Financial Review 57(2) — https://centaur.reading.ac.uk/100181/
- Wen, Bouri, Xu & Zhao (2022), NAJEF 62:101733 — https://ideas.repec.org/a/eee/ecofin/v62y2022ics1062940822000833.html
- Baur, Cahill, Godfrey & Liu (2019), FRL 31 — https://ideas.repec.org/a/eee/finlet/v31y2019icp78-92.html
- Caporale & Plastun (2019), FRL 31 — https://ideas.repec.org/a/eee/finlet/v31y2019ics1544612318304240.html
- Kim & Hansen (2026), *The Quarter-Hour Effect* — https://arxiv.org/abs/2607.09426
- Hansen, Kim & Kimbrough (2022), J. Financial Econometrics — https://arxiv.org/abs/2109.12142
- He, Manela, Ross & von Wachter, *Fundamentals of Perpetual Futures* — https://arxiv.org/abs/2212.06888
- Borri, Liu, Tsyvinski & Wu (2025) — https://arxiv.org/abs/2510.14435
- Miralles-Quirós & Miralles-Quirós (2022), J. Applied Economics 25(1) — https://ideas.repec.org/a/taf/recsxx/v25y2022i1p1294-1313.html
- Zaremba et al. (2021), IRFA 78 — https://ideas.repec.org/a/eee/finana/v78y2021ics1057521921002349.html
- Bieganowski & Ślepaczuk (2026) — https://arxiv.org/abs/2602.00776
- Mroziewicz & Ślepaczuk (2026) — https://arxiv.org/abs/2602.10785
