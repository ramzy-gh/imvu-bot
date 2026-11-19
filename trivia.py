"""Bilingual offline trivia with room-local, durable no-repeat history."""
import asyncio
import logging
import os
import random
from pathlib import Path
from state_store import StateStore

LOG = logging.getLogger('ramsei.trivia')
# category | Egyptian Arabic question | English question | Arabic answer aliases | English aliases
ROWS = '''geography|إيه عاصمة اليابان؟|What is Japan's capital?|طوكيو|Tokyo
geography|إيه عاصمة فرنسا؟|What is France's capital?|باريس|Paris
geography|إيه عاصمة إيطاليا؟|What is Italy's capital?|روما|Rome
geography|إيه عاصمة إسبانيا؟|What is Spain's capital?|مدريد|Madrid
geography|إيه عاصمة روسيا؟|What is Russia's capital?|موسكو|Moscow
geography|إيه عاصمة ألمانيا؟|What is Germany's capital?|برلين|Berlin
geography|إيه عاصمة بريطانيا؟|What is the UK's capital?|لندن|London
geography|إيه عاصمة كندا؟|What is Canada's capital?|أوتاوا;اوتاوا|Ottawa
geography|إيه عاصمة أستراليا؟|What is Australia's capital?|كانبرا;كانبيرا|Canberra
geography|إيه عاصمة المغرب؟|What is Morocco's capital?|الرباط;رباط|Rabat
geography|إيه عاصمة لبنان؟|What is Lebanon's capital?|بيروت|Beirut
geography|إيه عاصمة الأردن؟|What is Jordan's capital?|عمان;عمّان|Amman
geography|إيه أكبر محيط على الأرض؟|Which ocean is the largest?|المحيط الهادي;المحيط الهادئ;الهادي;الهادئ|Pacific;Pacific Ocean;the Pacific
geography|إيه أكبر قارة في المساحة؟|Which continent has the largest land area?|آسيا;اسيا|Asia
geography|إيه أصغر قارة من القارات السبع؟|Which of the seven continents is the smallest?|أستراليا;استراليا|Australia
geography|إيه المحيط اللي بين أفريقيا وأمريكا الجنوبية؟|Which ocean separates Africa and South America?|الأطلسي;المحيط الأطلسي;الاطلنطي;المحيط الاطلنطي|Atlantic;Atlantic Ocean
geography|إيه البحر اللي بين مصر والسعودية؟|Which sea separates Egypt and Saudi Arabia?|البحر الأحمر;الاحمر|Red Sea;the Red Sea
geography|برج إيفل موجود في أنهي مدينة؟|Which city is home to the Eiffel Tower?|باريس|Paris
geography|الكولوسيوم موجود في أنهي مدينة؟|Which city is home to the Colosseum?|روما|Rome
geography|جبل إيفرست موجود في أنهي سلسلة جبال؟|Which mountain range contains Mount Everest?|الهيمالايا;هيمالايا|Himalayas;the Himalayas
science|إيه الكوكب المشهور بالكوكب الأحمر؟|Which planet is known as the Red Planet?|المريخ;مريخ|Mars
science|إيه أكبر كوكب في مجموعتنا الشمسية؟|What is the largest planet in our Solar System?|المشتري;مشترى;مشتري|Jupiter
science|إيه أقرب كوكب للشمس؟|Which planet is closest to the Sun?|عطارد|Mercury
science|إيه الكوكب المشهور بحلقاته الواضحة؟|Which planet is famous for its prominent rings?|زحل|Saturn
science|إيه الغاز اللي جسمنا محتاجه من الهوا للتنفس؟|Which gas do our bodies need from the air to breathe?|الأكسجين;اكسجين;أوكسجين|oxygen
science|إيه الغاز اللي النبات بيمتصه في البناء الضوئي؟|Which gas do plants absorb during photosynthesis?|ثاني أكسيد الكربون;ثانى اكسيد الكربون|carbon dioxide;CO2
science|إيه العضو اللي بيضخ الدم في الجسم؟|Which organ pumps blood around the body?|القلب;قلب|heart;the heart
science|إيه أكبر عضو في جسم الإنسان؟|What is the human body's largest organ?|الجلد;جلد|skin;the skin
science|إيه العضو اللي بنستخدمه في التفكير؟|Which organ do we use to think?|المخ;مخ;الدماغ;دماغ|brain;the brain
science|المياه بتغلي عند كام درجة مئوية عند مستوى البحر؟|At sea level, at what temperature does water boil in Celsius?|100;١٠٠;مية;مائة|100;100 degrees
science|المياه النقية بتتجمد عند كام درجة مئوية في الضغط العادي؟|At normal pressure, at what Celsius temperature does pure water freeze?|0;٠;صفر|0;zero
science|إيه الرمز الكيميائي للذهب؟|What is the chemical symbol for gold?|Au|Au
science|إيه الرمز الكيميائي للحديد؟|What is the chemical symbol for iron?|Fe|Fe
science|إيه اسم تحول المياه من سائل لبخار؟|What is the change from liquid water to vapour called?|التبخر;تبخر|evaporation
science|إيه اسم القوة اللي بتشدنا ناحية الأرض؟|What force pulls us toward Earth?|الجاذبية;جاذبية|gravity
science|إيه الحيوان الثديي اللي بيطير طيران حقيقي؟|Which mammal is capable of powered flight?|الخفاش;خفاش;الوطواط;وطواط|bat;a bat
science|الأخطبوط عنده كام قلب؟|How many hearts does an octopus have?|3;٣;ثلاثة;تلاتة|3;three
science|الحشرة عندها كام رجل؟|How many legs does an insect have?|6;٦;ستة|6;six
science|إيه أصلب معدن طبيعي على مقياس موس؟|What is the hardest natural mineral on the Mohs scale?|الألماس;الماس;ألماس|diamond
science|إيه الأداة اللي بتقيس درجة الحرارة؟|What instrument measures temperature?|الترمومتر;ترمومتر;ميزان الحرارة|thermometer;a thermometer
culture|إيه اللعبة اللي فيها ملك ووزير وقلعة؟|Which board game has kings, queens and rooks?|الشطرنج;شطرنج|chess
culture|كام مربع في لوحة الشطرنج العادية؟|How many squares are on a standard chessboard?|64;٦٤;أربعة وستين;اربعة وستين|64;sixty four;sixty-four
culture|كام لاعب من كل فريق بيبدأ ماتش كرة القدم؟|How many players per team start an association football match?|11;١١;حداشر;أحد عشر|11;eleven
culture|كام حلقة في شعار الألعاب الأولمبية؟|How many rings are in the Olympic symbol?|5;٥;خمسة|5;five
culture|كام وتر في الجيتار العادي؟|How many strings does a standard guitar have?|6;٦;ستة|6;six
culture|كام ضلع في الشكل السداسي؟|How many sides does a hexagon have?|6;٦;ستة|6;six
culture|كام ضلع في الشكل الثماني؟|How many sides does an octagon have?|8;٨;تمانية;ثمانية|8;eight
culture|إيه مجموع زوايا المثلث بالدرجات؟|What is the sum of a triangle's angles in degrees?|180;١٨٠;مية وتمانين|180;180 degrees
culture|كام شهر في السنة؟|How many months are in a year?|12;١٢;اتناشر;اثنا عشر|12;twelve
culture|كام يوم في السنة الكبيسة؟|How many days are in a leap year?|366;٣٦٦|366
culture|كام ثانية في الدقيقة؟|How many seconds are in a minute?|60;٦٠;ستين|60;sixty
culture|كام قطعة في الدستة؟|How many items are in a dozen?|12;١٢;اتناشر|12;twelve
culture|في الكمبيوتر، إيه الاختصار الشائع لوحدة المعالجة المركزية؟|What is the usual abbreviation for central processing unit?|CPU|CPU
culture|إيه الاختصار الشائع لملفات المستندات المحمولة؟|What is the common abbreviation for Portable Document Format?|PDF|PDF
culture|مين كتب روميو وجولييت؟|Who wrote Romeo and Juliet?|شكسبير;وليام شكسبير;ويليام شكسبير|Shakespeare;William Shakespeare
culture|مين رسم الموناليزا؟|Who painted the Mona Lisa?|ليوناردو دافنشي;دافنشي;ليوناردو دا فينشي|Leonardo da Vinci;da Vinci
culture|إيه العملة المستخدمة في اليابان؟|What currency is used in Japan?|الين;ين;الين الياباني|yen;Japanese yen
culture|إيه اللغة الرسمية في البرازيل؟|What is Brazil's official language?|البرتغالية;برتغالي;اللغة البرتغالية|Portuguese
culture|بنتعلم رسم المجسمات ثلاثية الأبعاد في برنامج مشهور اسمه Blender.. معنى 3D إيه؟|In 3D modelling, what does the D stand for?|أبعاد;ابعاد;بعد|dimension;dimensions;dimensional
culture|إيه اسم الآلة الموسيقية اللي ليها مفاتيح بيضا وسودا واسمها يبدأ بحرف ب؟|Which musical instrument with black and white keys starts with P?|بيانو;البيانو|piano;a piano
riddles|حاجة ليها سنان بس مش بتعض، وبتسرح بيها شعرك؟|What has teeth but cannot bite, and is used to tidy hair?|المشط;مشط|comb;a comb
riddles|إيه اللي بيكبر كل ما تحفر وتاخد منه؟|What gets bigger the more you dig out of it?|الحفرة;حفرة;حفره|hole;a hole
riddles|إيه اللي بينشفك وهو نفسه بيبقى مبلول؟|What gets wetter while drying you?|الفوطة;فوطة;المنشفة;منشفة|towel;a towel
riddles|إيه اللي ليه رقبة ومش ليه راس وبتحط فيه مياه؟|What has a neck but no head and can hold water?|الزجاجة;زجاجة;ازازة;الإزازة|bottle;a bottle
riddles|إيه اللي ليه عين واحدة ومش بيشوف وبيخيط؟|What has one eye, cannot see, and is used for sewing?|الإبرة;ابرة;إبرة|needle;a needle
riddles|إيه اللي كله ثقوب ولسه بيحتفظ بالمياه؟|What is full of holes but still holds water?|الإسفنجة;اسفنجة;إسفنجة|sponge;a sponge
riddles|إيه اللي ليه مفاتيح ومسافة وEnter بس مفيش أبواب؟|What has keys, a space and Enter, but no doors?|لوحة المفاتيح;الكيبورد;كيبورد|keyboard;a keyboard
riddles|إيه اللي تقدر تكسره من غير ما تلمسه: وعد ولا حجر؟|What can you break without touching it: a promise or a stone?|الوعد;وعد|promise;a promise
riddles|إيه اللي بنشتريه عشان ناكل فيه، لكن مش بناكله؟|What do we buy to eat from, but do not eat?|الطبق;طبق;الصحن;صحن|plate;a plate
riddles|حاجة ليها وش وعقربين ومش إنسان؟|What has a face and two hands but is not a person?|الساعة;ساعة|clock;a clock;watch;a watch
riddles|كام شهر في السنة فيه 28 يوم على الأقل؟|How many months have at least 28 days?|12;١٢;كل الشهور;اتناشر|12;all;twelve;all months
riddles|لو معاك 3 تفاحات وأخدت منهم 2 في إيدك، معاك كام في إيدك؟|If there are 3 apples and you take 2, how many are in your hand?|2;٢;اتنين;اثنين|2;two
riddles|إيه الأتقل: كيلو حديد ولا كيلو قطن؟|Which is heavier: a kilogram of iron or a kilogram of cotton?|متساويين;نفس الوزن;زي بعض;قد بعض;الاتنين زي بعض|equal;same;the same;neither;same weight
riddles|إيه اللي بيتحرك حوالي الجنينة بس مش بيمشي؟|What goes around a garden but never moves?|السور;سور;السياج;سياج|fence;a fence;the fence
riddles|إيه اللي ليه أربع أرجل وسطح للأكل ومش بيمشي؟|What has four legs and a surface for meals, but cannot walk?|الترابيزة;ترابيزة;الطاولة;طاولة;منضدة|table;a table
riddles|حاجة بتكرر كلامك من غير بق ولا ودان؟|What repeats your voice without a mouth or ears?|الصدى;صدى;صدى الصوت|echo;an echo
riddles|إيه اللي بتسمعه لكن مش بتشوفه، وناتج عن اهتزازات؟|What can you hear but not see, and is produced by vibrations?|الصوت;صوت|sound
riddles|لو عديت اللي في المركز التاني في السباق، بقيت في أنهي مركز؟|If you overtake second place in a race, what place are you in?|التاني;الثاني;تاني;ثاني;2;٢|second;2;2nd
riddles|إيه اللي بنمشي عليه عشان نطلع أو ننزل بين أدوار العمارة؟|What do you step on to go up or down between floors?|السلم;سلم;الدرج;درج|stairs;the stairs;staircase
riddles|إيه اللي لازم تكسره عشان تستخدم اللي جواه في الأومليت؟|What must you break to use what is inside for an omelette?|البيضة;بيضة;البيض;بيض|egg;an egg;eggs'''

CATEGORIES = {'geography': 'جغرافيا', 'science': 'علوم', 'culture': 'معلومات', 'riddles': 'ألغاز', 'math': 'حساب'}
ALIASES = {**{v: k for k, v in CATEGORIES.items()}, 'جغرافيا': 'geography', 'الغاز': 'riddles',
           'رياضيات': 'math', 'عام': 'culture', 'general': 'culture', 'all': None, 'الكل': None, '': None}


def bank():
    result = []
    for index, line in enumerate(ROWS.splitlines()):
        category, ar, en, aa, ea = line.split('|')
        result.append({'id': 'q' + str(index), 'category': category, 'ar': ar, 'en': en,
                       'answers': aa.split(';') + ea.split(';'), 'display_ar': aa.split(';')[0], 'display_en': ea.split(';')[0]})
    for a in range(3, 23):
        for b in range(2, 10):
            result.append({'id': f'multiply-{a}-{b}', 'category': 'math',
                'ar': f'تحدي سريع: {a} × {b} = كام؟', 'en': f'Quick challenge: {a} × {b} = ?',
                'answers': [str(a*b)], 'display_ar': str(a*b), 'display_en': str(a*b)})
    return result


QUESTIONS = bank()


class TriviaDeck:
    def __init__(self, room=None):
        self.seen, self.last, self.loaded = set(), None, False
        self.store = (StateStore('trivia-' + room, Path(os.getenv('DATA_DIR', '/app/data')) / 'ramsei' / ('trivia-' + room + '.json'))
                      if room else None)

    async def next(self, category=None):
        if not self.loaded:
            self.loaded = True
            if self.store:
                try:
                    data = await asyncio.to_thread(self.store.load)
                    if isinstance(data, dict):
                        valid = {q['id'] for q in QUESTIONS}
                        self.seen = set(x for x in data.get('seen', []) if isinstance(x, str) and x in valid)
                        self.last = data.get('last')
                except (OSError, ValueError, TypeError):
                    LOG.warning('Trivia history unavailable; using in-memory repeat prevention')
        pool = [q for q in QUESTIONS if category is None or q['category'] == category]
        available = [q for q in pool if q['id'] not in self.seen]
        if not available:
            self.seen.difference_update(q['id'] for q in pool)
            available = [q for q in pool if q['id'] != self.last] or pool
        # Equal category chances prevent the generated math bank dominating mixed games.
        chosen_category = random.choice(sorted({q['category'] for q in available}))
        question = random.choice([q for q in available if q['category'] == chosen_category])
        self.seen.add(question['id'])
        self.last = question['id']
        if self.store:
            try:
                await asyncio.to_thread(self.store.save, {'seen': sorted(self.seen), 'last': self.last})
            except OSError:
                LOG.warning('Trivia history save unavailable; in-memory repeat prevention remains active')
        return question
