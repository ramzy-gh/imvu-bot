"""Language preferences for the Ramzy Bots website."""
import re
from contextvars import ContextVar
from html import escape, unescape

from aiohttp import web

LANGUAGES = {"en": "English", "ar": "العربية", "ru": "Русский"}
RETURN_PATHS = {"/", "/guide", "/signin", "/signup", "/verify-email", "/forgot-password",
                "/reset-password", "/account", "/panel", "/payments", "/owner", "/owner/payments"}
locale = ContextVar("website_locale", default=("en", "/"))

# Each entry supplies Arabic and Russian in that order.
TEXT = {
    "Command guide": ("دليل الأوامر", "Справочник команд"),
    "Control panel": ("لوحة التحكم", "Панель управления"),
    "Room": ("الغرفة", "Комната"),
    "Website language": ("لغة الموقع", "Язык сайта"),
    "Apply": ("تطبيق", "Применить"),
    "Owner panel": ("لوحة المالك", "Панель владельца"),
    "Admin panel": ("لوحة الإدارة", "Панель администратора"),
    "Find rooms": ("ابحث عن الغرف", "Найти комнаты"),
    "Room name, ID, or customer email": ("اسم الغرفة أو معرّفها أو بريد العميل", "Название, ID комнаты или почта клиента"),
    "Room name or ID": ("اسم الغرفة أو معرّفها", "Название или ID комнаты"),
    "Room status": ("حالة الغرفة", "Статус комнаты"),
    "All statuses": ("كل الحالات", "Все статусы"),
    "Apply filters": ("طبّق عوامل التصفية", "Применить фильтры"),
    "Clear filters": ("امسح عوامل التصفية", "Сбросить фильтры"),
    "Matching rooms": ("الغرف المطابقة", "Подходящие комнаты"),
    "No rooms match these filters.": ("لا توجد غرف تطابق عوامل التصفية.", "Нет комнат, подходящих под эти фильтры."),
    "Expired": ("منتهي", "Срок истёк"),
    "No expiration": ("بدون انتهاء", "Бессрочно"),
    "Access through (UTC)": ("الوصول حتى (UTC)", "Доступ до (UTC)"),
    "Last day of access (UTC)": ("آخر يوم للوصول (UTC)", "Последний день доступа (UTC)"),
    "Set expiration": ("تعيين تاريخ الانتهاء", "Установить срок"),
    "Set no expiration": ("تعيين وصول بلا انتهاء", "Сделать доступ бессрочным"),
    "No customer assigned": ("لم يُعيّن عميل", "Клиент не назначен"),
    "Website moderators": ("مشرفو الموقع", "Модераторы сайта"),
    "Registered account email": ("بريد الحساب المسجّل", "Почта зарегистрированного аккаунта"),
    "Select an account": ("اختر حسابًا", "Выберите аккаунт"),
    "No other registered accounts to add.": ("لا توجد حسابات مسجّلة أخرى لإضافتها.", "Других зарегистрированных аккаунтов для добавления нет."),
    "Add website moderator": ("إضافة مشرف للموقع", "Добавить модератора сайта"),
    "Remove website moderator": ("إزالة مشرف الموقع", "Удалить модератора сайта"),
    "What Quiet mode does": ("كيف يعمل الوضع الهادئ", "Как работает тихий режим"),
    "The bot responds to direct mentions and chat commands. It skips unaddressed conversation follow-ups. Some plain-chat game answers can also be skipped, so use the answer command for games. Music playback and automatic welcomes have their own controls.": (
        "يرد البوت على المنشن المباشر وأوامر الدردشة ويتجاهل متابعة المحادثات غير الموجّهة إليه. قد يتجاهل أيضًا بعض إجابات الألعاب المكتوبة كنص عادي، لذا استخدم أمر الإجابة. للموسيقى والترحيب التلقائي إعدادات مستقلة.",
        "Бот отвечает на прямые упоминания и команды и пропускает реплики без обращения к нему. Обычные сообщения с ответами в играх тоже могут быть пропущены, поэтому используйте команду ответа. Музыка и автоматические приветствия настраиваются отдельно."),
    "Customer view": ("عرض العميل", "Режим клиента"),
    "IMVU room companion": ("بوت غرف IMVU", "Бот для комнат IMVU"),
    "Ramzy Bots · IMVU room companion": ("Ramzy Bots · بوت غرف IMVU", "Ramzy Bots · бот для комнат IMVU"),
    "RAMZY BOTS / DASHBOARD": ("RAMZY BOTS / لوحة التحكم", "RAMZY BOTS / ПАНЕЛЬ"),
    "Sign in": ("تسجيل الدخول", "Войти"),
    "Sign out": ("تسجيل الخروج", "Выйти"),
    "Create account": ("إنشاء حساب", "Создать аккаунт"),
    "Hello": ("مرحبًا", "Здравствуйте"),
    "Hello,": ("مرحبًا،", "Здравствуйте,"),
    "Account settings": ("إعدادات الحساب", "Настройки аккаунта"),
    "Open control panel": ("افتح لوحة التحكم", "Открыть панель управления"),
    "Display name": ("الاسم الظاهر", "Отображаемое имя"),
    "Choose a display name of 1 to 40 characters.": ("اختر اسمًا ظاهرًا من حرف إلى 40 حرفًا.", "Выберите отображаемое имя от 1 до 40 символов."),
    "Save name": ("احفظ الاسم", "Сохранить имя"),
    "Display name updated.": ("تم تحديث الاسم الظاهر.", "Отображаемое имя обновлено."),
    "Current email": ("البريد الإلكتروني الحالي", "Текущий адрес электронной почты"),
    "New email": ("البريد الإلكتروني الجديد", "Новый адрес электронной почты"),
    "Send code to current email": ("أرسل الرمز إلى البريد الحالي", "Отправить код на текущую почту"),
    "Changing your email requires a code from both addresses.": ("يتطلب تغيير بريدك الإلكتروني رمزًا من كل عنوان.", "Для смены почты нужен код с каждого адреса."),
    "Enter the code sent to your current email.": ("أدخل الرمز المرسل إلى بريدك الحالي.", "Введите код, отправленный на текущую почту."),
    "Enter the code sent to your new email.": ("أدخل الرمز المرسل إلى بريدك الجديد.", "Введите код, отправленный на новую почту."),
    "Your Ramzy Bots email change code": ("رمز تغيير البريد الإلكتروني في Ramzy Bots", "Ваш код смены почты Ramzy Bots"),
    "Confirm code": ("أكّد الرمز", "Подтвердить код"),
    "Enter a different email address.": ("أدخل عنوان بريد إلكتروني آخر.", "Введите другой адрес электронной почты."),
    "That email address is already in use.": ("عنوان البريد الإلكتروني هذا مستخدم بالفعل.", "Этот адрес электронной почты уже используется."),
    "Email updated.": ("تم تحديث البريد الإلكتروني.", "Адрес электронной почты обновлён."),
    "Try again in a minute.": ("حاول مجددًا بعد دقيقة.", "Повторите попытку через минуту."),
    "Verify your email": ("أكّد بريدك الإلكتروني", "Подтвердите электронную почту"),
    "Verification code": ("رمز التحقق", "Код подтверждения"),
    "Verify email": ("تأكيد البريد الإلكتروني", "Подтвердить почту"),
    "Request a new code": ("اطلب رمزًا جديدًا", "Запросить новый код"),
    "Enter the six-digit code we sent to your email.": ("أدخل الرمز المكوّن من ستة أرقام الذي أرسلناه إلى بريدك.", "Введите шестизначный код из письма."),
    "Your Ramzy Bots verification code": ("رمز التحقق من Ramzy Bots", "Ваш код подтверждения Ramzy Bots"),
    "This code expires in 10 minutes.": ("تنتهي صلاحية هذا الرمز بعد 10 دقائق.", "Код действует 10 минут."),
    "Enter a valid email address.": ("أدخل عنوان بريد إلكتروني صالحًا.", "Введите действительный адрес электронной почты."),
    "We couldn't send a code right now. Try again later.": ("تعذر إرسال الرمز الآن. حاول مرة أخرى لاحقًا.", "Не удалось отправить код. Попробуйте позже."),
    "Wait a minute before requesting another code.": ("انتظر دقيقة قبل طلب رمز جديد.", "Подождите минуту перед повторным запросом кода."),
    "This code has expired or is no longer available. Request a new code.": ("انتهت صلاحية الرمز أو لم يعد متاحًا. اطلب رمزًا جديدًا.", "Срок действия кода истёк или код недоступен. Запросите новый код."),
    "That code is incorrect. Try again.": ("الرمز غير صحيح. حاول مرة أخرى.", "Неверный код. Попробуйте ещё раз."),
    "Email": ("البريد الإلكتروني", "Электронная почта"),
    "Password": ("كلمة المرور", "Пароль"),
    "Change password": ("غيّر كلمة المرور", "Изменить пароль"),
    "Forgot password?": ("هل نسيت كلمة المرور؟", "Забыли пароль?"),
    "Back to sign in": ("العودة إلى تسجيل الدخول", "Вернуться ко входу"),
    "Enter your verified account email to request a reset code.": (
        "أدخل بريد حسابك المؤكّد لطلب رمز إعادة التعيين.",
        "Введите подтверждённый адрес аккаунта, чтобы запросить код сброса."),
    "Send reset code": ("أرسل رمز إعادة التعيين", "Отправить код сброса"),
    "Reset password": ("أعد تعيين كلمة المرور", "Сбросить пароль"),
    "If the address can be used for recovery, you will receive a six-digit code.": (
        "إذا كان العنوان متاحًا لاسترداد الحساب، فسيصلك رمز من ستة أرقام.",
        "Если адрес подходит для восстановления, на него придёт шестизначный код."),
    "The reset code is invalid or expired.": (
        "رمز إعادة التعيين غير صالح أو منتهي الصلاحية.", "Код сброса недействителен или просрочен."),
    "Your Ramzy Bots password reset code": (
        "رمز إعادة تعيين كلمة مرور Ramzy Bots", "Ваш код сброса пароля Ramzy Bots"),
    "Password updated. Sign in with your new password.": (
        "تم تحديث كلمة المرور. سجّل الدخول بكلمة المرور الجديدة.", "Пароль обновлён. Войдите с новым паролем."),
    "Your Ramzy Bots password was reset": (
        "تمت إعادة تعيين كلمة مرور Ramzy Bots", "Ваш пароль Ramzy Bots был сброшен"),
    "Your password was reset and your previous sessions were signed out.": (
        "تمت إعادة تعيين كلمة مرورك وتسجيل الخروج من الجلسات السابقة.",
        "Пароль был сброшен, а предыдущие сеансы завершены."),
    "Verify current email": ("أكّد البريد الإلكتروني الحالي", "Подтвердить текущую почту"),
    "Verify your current email to enable password recovery and account emails.": (
        "أكّد بريدك الإلكتروني الحالي لتفعيل استرداد كلمة المرور ورسائل الحساب.",
        "Подтвердите текущую почту, чтобы получать письма аккаунта и восстанавливать пароль."),
    "Email verified.": ("تم تأكيد البريد الإلكتروني.", "Адрес электронной почты подтверждён."),
    "Current password": ("كلمة المرور الحالية", "Текущий пароль"),
    "New password": ("كلمة المرور الجديدة", "Новый пароль"),
    "Confirm new password": ("أكّد كلمة المرور الجديدة", "Подтвердите новый пароль"),
    "Password updated.": ("تم تحديث كلمة المرور.", "Пароль обновлён."),
    "Choose a new password of 12 to 256 characters.": (
        "اختر كلمة مرور جديدة من 12 إلى 256 حرفًا.", "Выберите новый пароль длиной от 12 до 256 символов."),
    "The new passwords do not match.": ("كلمتا المرور الجديدتان غير متطابقتين.", "Новые пароли не совпадают."),
    "Current password is incorrect.": ("كلمة المرور الحالية غير صحيحة.", "Текущий пароль введён неверно."),
    "Choose a password different from your current password.": (
        "اختر كلمة مرور تختلف عن الحالية.", "Выберите пароль, который отличается от текущего."),
    "Beta invite code": ("رمز دعوة النسخة التجريبية", "Код приглашения в бета-версию"),
    "Already have an account?": ("هل لديك حساب بالفعل؟", "Уже есть аккаунт?"),
    "Join the private beta": ("انضم إلى النسخة التجريبية الخاصة", "Присоединиться к закрытой бета-версии"),
    "Email or password is incorrect.": ("البريد الإلكتروني أو كلمة المرور غير صحيحة.", "Неверная почта или пароль."),
    "An active beta invite code is required.": ("يلزم رمز دعوة صالح للنسخة التجريبية.", "Нужен действующий код приглашения."),
    "Enter a valid email and a password of 12 to 256 characters.": ("أدخل بريدًا صالحًا وكلمة مرور من 12 إلى 256 حرفًا.", "Введите корректную почту и пароль длиной от 12 до 256 символов."),
    "Sign in to continue.": ("سجّل الدخول للمتابعة.", "Войдите, чтобы продолжить."),
    "PRIVATE BETA FOR IMVU ROOM HOSTS": ("نسخة تجريبية خاصة لأصحاب غرف IMVU", "ЗАКРЫТАЯ БЕТА ДЛЯ ВЛАДЕЛЬЦЕВ КОМНАТ IMVU"),
    "FOR IMVU ROOM HOSTS": ("لأصحاب غرف IMVU", "ДЛЯ ВЛАДЕЛЬЦЕВ КОМНАТ IMVU"),
    "Turn up your room.": ("علّي صوت غرفتك.", "Сделайте комнату громче."),
    "Play music, welcome guests, and start games in your IMVU room. Manage it all from Ramzy Bots.": (
        "شغّل الموسيقى، ورحّب بالضيوف، وابدأ الألعاب في غرفتك على IMVU. أدِر كل ذلك من Ramzy Bots.",
        "Включайте музыку, встречайте гостей и запускайте игры в своей комнате IMVU. Управляйте всем через Ramzy Bots."),
    "Join with an invite code": ("انضم برمز دعوة", "Войти по коду приглашения"),
    "Read the command guide": ("اقرأ دليل الأوامر", "Открыть справочник команд"),
    "Room radio": ("راديو الغرفة", "Радио комнаты"),
    "Welcomes and games": ("الترحيب والألعاب", "Приветствия и игры"),
    "Room controls": ("إدارة الغرفة", "Управление комнатой"),
    "See connection status, choose the reply language, and switch welcomes or quiet mode on or off. The service owner assigns website admins separately for each room.": (
        "تابع حالة الاتصال واختر لغة الردود وفعّل الترحيب أو الوضع الهادئ. يعيّن مالك الخدمة مشرفي الموقع لكل غرفة على حدة.",
        "Проверяйте подключение, выбирайте язык ответов, включайте приветствия или тихий режим. Владелец сервиса назначает администраторов отдельно для каждой комнаты."),
    "How to get a room connected": ("كيفية ربط الغرفة", "Как подключить комнату"),
    "Create an account, verify your email, and sign in.": ("أنشئ حسابًا وأكّد بريدك الإلكتروني ثم سجّل الدخول.", "Создайте аккаунт, подтвердите почту и войдите."),
    "Enter your IMVU room ID or link in the control panel.": (
        "أدخل معرّف غرفة IMVU أو رابطها في لوحة التحكم.", "Введите ID или ссылку на комнату IMVU в панели управления."),
    "Choose a room and report your transfer. Once the site owner confirms payment, you can manage that room from this panel.": (
        "اختر غرفة وأبلغ عن التحويل. بعد تأكيد الدفع من مالك الموقع، يمكنك إدارة تلك الغرفة من هذه اللوحة.",
        "Выберите комнату и сообщите о переводе. После подтверждения оплаты владельцем сайта вы сможете управлять этой комнатой из панели."),
    "Existing VCOIN orders": ("طلبات VCOIN الحالية", "Текущие заказы VCOIN"),
    "Payment history": ("سجل المدفوعات", "История платежей"),
    "No reported or confirmed payments yet.": (
        "لا توجد مدفوعات مُبلّغ عنها أو مؤكدة بعد.", "Заявленных или подтверждённых платежей пока нет."),
    "Payment confirmed": ("تم تأكيد الدفع", "Платёж подтверждён"),
    "Confirmed on": ("تاريخ التأكيد", "Дата подтверждения"),
    "Access renewed until": ("تم تجديد الوصول حتى", "Доступ продлён до"),
    "Date not recorded": ("التاريخ غير مسجّل", "Дата не сохранена"),
    "Newer payments": ("مدفوعات أحدث", "Более новые платежи"),
    "Older payments": ("مدفوعات أقدم", "Более ранние платежи"),
    "This account has already used a free trial.": (
        "استخدم هذا الحساب التجربة المجانية بالفعل.", "Этот аккаунт уже использовал пробный период."),
    "Start your free 3-day trial. It begins when the bot connects.": ("ابدأ تجربتك المجانية لمدة 3 أيام عند اتصال البوت.", "Запустите бесплатный пробный период на 3 дня. Он начнётся после подключения бота."),
    "Continue with VCOIN": ("تابع باستخدام VCOIN", "Продлить за VCOIN"),
    "700 VCOIN for 30 days or 6,720 VCOIN for one year. The yearly plan saves 20%.": (
        "700 VCOIN لمدة 30 يومًا أو 6,720 VCOIN لمدة سنة. توفر الخطة السنوية 20٪.",
        "700 VCOIN за 30 дней или 6 720 VCOIN за год. Скидка на годовой план — 20%."),
    "The bot leaves when access ends and reconnects after a renewal is confirmed.": (
        "يغادر البوت عند انتهاء الوصول ويعاود الاتصال بعد تأكيد التجديد.",
        "Бот покидает комнату по окончании доступа и возвращается после подтверждения продления."),
    "Free 3-day trial": ("تجربة مجانية لمدة 3 أيام", "Бесплатная пробная версия на 3 дня"),
    "One trial per account. It starts when the bot connects to your room.": (
        "تجربة واحدة لكل حساب. تبدأ عند اتصال البوت بغرفتك.", "Один пробный период на аккаунт. Он начинается при подключении бота к комнате."),
    "Start trial": ("ابدأ التجربة", "Начать пробный период"),
    "Preparing trial": ("جارٍ تجهيز التجربة", "Подготовка пробного периода"),
    "Trial paused": ("التجربة متوقفة مؤقتًا", "Пробный период приостановлен"),
    "Trial ends": ("تنتهي التجربة في", "Пробный период до"),
    "Your trial starts when the bot connects. Make sure it can enter your room.": (
        "تبدأ التجربة عند اتصال البوت. تأكّد من أنه يستطيع دخول غرفتك.",
        "Пробный период начнётся при подключении бота. Убедитесь, что бот может войти в комнату."),
    "Active until": ("نشط حتى", "Действует до"),
    "Expired on": ("انتهى في", "Истёк"),
    "VCOIN subscriptions": ("اشتراكات VCOIN", "Подписки за VCOIN"),
    "VCOIN checkout is not available yet.": ("الدفع بـ VCOIN غير متاح بعد.", "Оплата VCOIN пока недоступна."),
    "Subscription plan": ("خطة الاشتراك", "План подписки"),
    "30 days · 700 VCOIN": ("30 يومًا · 700 VCOIN", "30 дней · 700 VCOIN"),
    "1 year · 6,720 VCOIN · save 20%": ("سنة واحدة · 6,720 VCOIN · وفر 20٪", "1 год · 6 720 VCOIN · скидка 20%"),
    "Create VCOIN order": ("أنشئ طلب VCOIN", "Создать заказ VCOIN"),
    "VCOIN order": ("طلب VCOIN", "Заказ VCOIN"),
    "Reported by IMVU user": ("أبلغ عنه مستخدم IMVU", "Указанный отправитель IMVU"),
    "Awaiting wallet verification": ("بانتظار التحقق في المحفظة", "Ожидает сверки с кошельком"),
    "Send the exact order amount in IMVU to": ("أرسل مبلغ الطلب بالضبط في IMVU إلى", "Отправьте точную сумму заказа в IMVU получателю"),
    ". Include this order code in the transfer note.": (". أضف رمز الطلب إلى ملاحظة التحويل.", ". Укажите код заказа в примечании к переводу."),
    "IMVU deducts a 5% transfer fee from the amount sent.": ("تخصم IMVU رسوم تحويل قدرها 5٪ من المبلغ المرسل.", "IMVU удерживает комиссию 5% из отправленной суммы."),
    "Open VCOIN transfer in IMVU": ("افتح تحويل VCOIN في IMVU", "Открыть перевод VCOIN в IMVU"),
    "Your sending IMVU username": ("اسم مستخدم IMVU الذي أرسل التحويل", "Ваше имя пользователя IMVU при отправке"),
    "Report transfer": ("أبلغ عن التحويل", "Сообщить о переводе"),
    "Cancel unpaid order": ("ألغِ الطلب غير المدفوع", "Отменить неоплаченный заказ"),
    "Expected receipt after IMVU’s fee": ("المبلغ المتوقع بعد رسوم IMVU", "Ожидаемая сумма после комиссии IMVU"),
    "I matched the sender, amount, and order code in my IMVU Wallet.": (
        "طابقت المرسل والمبلغ ورمز الطلب في محفظة IMVU.", "Я сверил отправителя, сумму и код заказа в кошельке IMVU."),
    "Wallet transaction reference": ("مرجع المعاملة في المحفظة", "Номер транзакции в кошельке"),
    "Confirm VCOIN transfer": ("أكّد تحويل VCOIN", "Подтвердить перевод VCOIN"),
    "Restore access": ("استعد الوصول", "Восстановить доступ"),
    "No rooms yet.": ("لا توجد غرف بعد.", "Комнат пока нет."),
    "Manage your room": ("إدارة غرفتك", "Управление вашей комнатой"),
    "Website room admins can change settings only in assigned rooms. Room access follows the trial or subscription.": (
        "يمكن لمشرفي الغرف تغيير إعدادات الغرف المعيّنة لهم فقط. يتبع الوصول فترة التجربة أو الاشتراك.",
        "Администраторы меняют настройки только в назначенных комнатах. Доступ зависит от пробного периода или подписки."),
    "Create a beta account with an invite code and sign in.": ("أنشئ حسابًا تجريبيًا برمز الدعوة وسجّل الدخول.", "Создайте бета-аккаунт по коду приглашения и войдите."),
    "Submit your IMVU room ID or room link from the control panel.": ("أرسل معرّف غرفة IMVU أو رابطها من لوحة التحكم.", "Отправьте ID или ссылку на комнату IMVU через панель управления."),
    "After the service owner approves the request and connects the bot, check that the room shows": ("بعد موافقة مالك الخدمة وتوصيل البوت، تأكد من ظهور حالة", "После одобрения заявки и подключения бота проверьте статус"),
    "Connected": ("متصل", "Подключено"),
    "Connecting": ("جارٍ الاتصال", "Подключение"),
    "Offline": ("غير متصل", "Не в сети"),
    "Room health": ("حالة اتصال الغرفة", "Состояние подключения комнаты"),
    "Last connected": ("آخر اتصال", "Последнее подключение"),
    "Last disconnect reason": ("سبب آخر انقطاع", "Причина последнего отключения"),
    "No connection recorded yet": ("لم يُسجّل أي اتصال بعد", "Подключений пока не зарегистрировано"),
    "No disconnect recorded yet": ("لم يُسجّل أي انقطاع بعد", "Отключений пока не зарегистрировано"),
    "The bot was disconnected by a room administrator.": (
        "فصل أحد مشرفي الغرفة البوت.", "Администратор комнаты отключил бота."),
    "Room access expired.": ("انتهت صلاحية الوصول إلى الغرفة.", "Срок доступа к комнате истёк."),
    "Room access was suspended.": ("عُلّق الوصول إلى الغرفة.", "Доступ к комнате приостановлен."),
    "IMVU denied access or the room is unavailable.": (
        "رفض IMVU الوصول أو أن الغرفة غير متاحة.", "IMVU отклонил доступ или комната недоступна."),
    "The bot could not sign in to IMVU.": ("تعذّر على البوت تسجيل الدخول إلى IMVU.", "Боту не удалось войти в IMVU."),
    "The IMVU session expired.": ("انتهت صلاحية جلسة IMVU.", "Сессия IMVU истекла."),
    "The bot could not connect to the room.": ("تعذّر على البوت الاتصال بالغرفة.", "Боту не удалось подключиться к комнате."),
    "The room connection to IMVU was interrupted.": (
        "انقطع اتصال الغرفة بـ IMVU.", "Подключение комнаты к IMVU прервалось."),
    "The IMVU connection timed out.": (
        "انتهت مهلة الاتصال بـ IMVU.", "Истекло время ожидания подключения к IMVU."),
    "The room connection stopped unexpectedly.": (
        "توقف اتصال الغرفة بشكل غير متوقع.", "Подключение к комнате неожиданно прекратилось."),
    "The bot service stopped or restarted.": (
        "توقفت خدمة البوت أو أُعيد تشغيلها.", "Служба бота остановилась или перезапустилась."),
    "Pending": ("قيد المراجعة", "На рассмотрении"),
    "Suspended": ("موقوف", "Приостановлено"),
    "Approved": ("معتمد", "Одобрено"),
    "Request a room": ("طلب ربط غرفة", "Заявка на комнату"),
    "IMVU room ID or link": ("معرّف غرفة IMVU أو رابطها", "ID или ссылка на комнату IMVU"),
    "Request access": ("طلب الوصول", "Запросить доступ"),
    "No room requests yet.": ("لا توجد طلبات غرف بعد.", "Заявок на комнаты пока нет."),
    "Bot language": ("لغة البوت", "Язык бота"),
    "Automatic": ("تلقائي", "Автоматически"),
    "English": ("الإنجليزية", "Английский"),
    "Egyptian Arabic": ("العربية المصرية", "Египетский арабский"),
    "Russian": ("الروسية", "Русский"),
    "Save language": ("حفظ اللغة", "Сохранить язык"),
    "Welcome message": ("رسالة الترحيب", "Приветствие"),
    "Default welcome preview": ("معاينة الترحيب الافتراضي", "Предпросмотр стандартного приветствия"),
    "Custom welcome preview": ("معاينة الترحيب المخصّص", "Предпросмотр своего приветствия"),
    "Welcomes are off for this room.": ("الترحيب متوقف في هذه الغرفة.", "Приветствия для этой комнаты выключены."),
    "Leave blank to use the default. Use {name} for the visitor and {room} for the room.": (
        "اترك الحقل فارغًا لاستخدام الرسالة الافتراضية. استخدم {name} للزائر و{room} للغرفة.",
        "Оставьте поле пустым для стандартного текста. Используйте {name} для гостя и {room} для комнаты."),
    "Welcome guests": ("الترحيب بالزوار", "Приветствовать гостей"),
    "Quiet mode": ("الوضع الهادئ", "Тихий режим"),
    "Save settings": ("حفظ الإعدادات", "Сохранить настройки"),
    "Room radio stream": ("بث راديو الغرفة", "Поток радио комнаты"),
    "Radio setup": ("إعداد الراديو", "Настройка радио"),
    "Approve room": ("الموافقة على الغرفة", "Одобрить комнату"),
    "Decline request": ("رفض الطلب", "Отклонить заявку"),
    "Suspend access": ("إيقاف الوصول", "Приостановить доступ"),
    "Remove room": ("احذف الغرفة", "Удалить комнату"),
    "This disconnects the bot and permanently deletes this room’s saved settings, access and orders.": (
        "سيغادر البوت الغرفة وستُحذف إعداداتها المحفوظة وصلاحياتها وطلباتها نهائيًا.",
        "Бот покинет комнату, а её настройки, права доступа и заказы будут удалены навсегда."),
    "Type the room ID to confirm": ("اكتب معرّف الغرفة للتأكيد", "Введите ID комнаты для подтверждения"),
    "Remove room permanently": ("احذف الغرفة نهائيًا", "Удалить комнату навсегда"),
    "Type the exact room ID to confirm removal.": (
        "اكتب معرّف الغرفة بالضبط لتأكيد حذفها.", "Введите точный ID комнаты, чтобы подтвердить удаление."),
    "Room removal could not finish. The room is suspended. Try removing it again.": (
        "لم يكتمل حذف الغرفة. أُوقف الوصول إليها. حاول حذفها مجددًا.",
        "Не удалось завершить удаление. Доступ к комнате приостановлен. Попробуйте удалить её ещё раз."),
    "Connect bot": ("توصيل البوت", "Подключить бота"),
    "Disconnect bot": ("فصل البوت", "Отключить бота"),
    "Room admins": ("مشرفو الغرفة", "Администраторы комнаты"),
    "Admin account email": ("بريد حساب المشرف", "Почта аккаунта администратора"),
    "Add room admin": ("إضافة مشرف للغرفة", "Добавить администратора"),
    "Remove admin": ("إزالة المشرف", "Удалить администратора"),
    "Recent activity": ("آخر النشاطات", "Последние действия"),
    "No activity recorded yet.": ("لا توجد نشاطات مسجّلة بعد.", "Действий пока нет."),
    "Room removed": ("أُزيلت الغرفة", "Комната удалена"),
    "Room update": ("تحديث الغرفة", "Обновление комнаты"),
    "Admin update": ("تحديث الإدارة", "Изменение доступа"),
    "Payment update": ("تحديث الدفع", "Обновление платежа"),
    "VCOIN payment inbox": ("طلبات دفع VCOIN", "Очередь платежей VCOIN"),
    "No reported VCOIN transfers are waiting for review.": (
        "لا توجد تحويلات VCOIN مُبلّغ عنها بانتظار المراجعة.", "Нет заявленных переводов VCOIN, ожидающих проверки."),
    "IMVU room ID": ("معرّف الغرفة في IMVU", "ID комнаты IMVU"),
    "Restore this room before confirming its payment.": (
        "أعِد تفعيل هذه الغرفة قبل تأكيد الدفع لها.", "Восстановите доступ к комнате, прежде чем подтверждать платёж."),
    "This transfer report cannot be changed.": ("لا يمكن تعديل بلاغ التحويل هذا.", "Это сообщение о переводе нельзя изменить."),
    "Return transfer report for correction": ("أعد بلاغ التحويل للتصحيح", "Вернуть сообщение о переводе на исправление"),
    "Moderators can manage settings and connect or disconnect the bot in approved rooms. Only the owner can assign access or change expiration dates.": (
        "يمكن للمشرفين إدارة الإعدادات وتوصيل البوت أو فصله في الغرف المعتمدة. المالك وحده يمكنه تعيين الوصول أو تغيير تواريخ الانتهاء.",
        "Модераторы могут менять настройки и подключать или отключать бота в одобренных комнатах. Только владелец назначает доступ и меняет сроки."),
    "Enter the email of a registered account other than the room customer.": (
        "أدخل بريد حساب مسجّل غير حساب عميل الغرفة.", "Введите почту зарегистрированного аккаунта, отличного от клиента комнаты."),
    "for a reply. Choose the bot’s reply language in your room settings.": (
        "للحصول على رد. اختر لغة ردود البوت من إعدادات غرفتك.", "для ответа. Выберите язык ответов бота в настройках комнаты."),
    "Back to panel": ("العودة إلى اللوحة", "Вернуться в панель"),
    "Enter the email of a registered beta account other than the room customer.": (
        "أدخل بريد حساب تجريبي مسجّل يختلف عن حساب صاحب الغرفة.",
        "Укажите почту зарегистрированного бета-аккаунта, отличного от аккаунта клиента этой комнаты."),
    "Set up the room radio": ("إعداد راديو الغرفة", "Настройка радио комнаты"),
    "Play in chat": ("اللعب في الدردشة", "Игры в чате"),
    "Chat, move, and request an invite": ("الدردشة والحركة وطلب دعوة", "Чат, перемещение и приглашения"),
    "Manage an approved room": ("إدارة غرفة معتمدة", "Управление одобренной комнатой"),
    "Choose the bot’s language from the room’s Language menu in the control panel.": (
        "اختر لغة البوت من قائمة اللغة الخاصة بالغرفة في لوحة التحكم.",
        "Выберите язык бота в настройках комнаты в панели управления."),
    "Type these commands in an IMVU room where the bot is connected. Use": (
        "اكتب هذه الأوامر في غرفة IMVU متصل بها البوت. استخدم",
        "Введите эти команды в комнате IMVU, к которой подключён бот. Используйте"),
    "for a quick list in chat.": ("لعرض قائمة سريعة في الدردشة.", "для краткого списка в чате."),
    "Type": ("اكتب", "Введите"),
    "in the room to get its MP3 link and listener count.": (
        "في الغرفة لعرض رابط MP3 وعدد المستمعين.", "в комнате, чтобы получить ссылку MP3 и число слушателей."),
    "In IMVU Classic, the room host or moderator opens Radio, loads that link, and presses Play.": (
        "في IMVU Classic، يفتح المضيف أو المشرف الراديو ويضع الرابط ويضغط تشغيل.",
        "В IMVU Classic владелец комнаты или модератор открывает радио, вставляет ссылку и нажимает Play."),
    "in room chat. If a numbered list appears, reply with the number within two minutes. You can also submit a supported HTTPS track link.": (
        "في دردشة الغرفة. إذا ظهرت قائمة مرقمة، أرسل الرقم خلال دقيقتين. يمكنك أيضًا إرسال رابط HTTPS مدعوم للأغنية.",
        "в чате комнаты. Если появится список, ответьте номером в течение двух минут. Можно также отправить поддерживаемую HTTPS-ссылку на трек."),
    "Each room has its own stream and queue. A song request adds a track to the queue.": (
        "لكل غرفة بث وقائمة تشغيل منفصلان. طلب الأغنية يضيفها إلى القائمة.",
        "У каждой комнаты свой поток и очередь. Запрос добавляет трек в очередь."),
    "shows upcoming songs,": ("يعرض الأغاني التالية،", "показывает очередь,"),
    "shows the current track,": ("يعرض الأغنية الحالية،", "показывает текущий трек,"),
    "advances playback, and": ("ينتقل للأغنية التالية، و", "переключает трек, а"),
    "stops playback and clears the queue. Owner-requested tracks have skip and stop protection.": (
        "يوقف التشغيل ويمسح القائمة. الأغاني التي يطلبها المالك محمية من التخطي والإيقاف.",
        "останавливает музыку и очищает очередь. Треки, заказанные владельцем, защищены от пропуска и остановки."),
    "starts trivia or a riddle. Answer in room chat or use": (
        "يبدأ سؤالًا أو لغزًا. أجب في الدردشة أو استخدم", "запускает викторину или загадку. Ответьте в чате или используйте"),
    "starts a round from 1 to 30. The bot gives higher or lower hints.": (
        "يبدأ جولة تخمين من 1 إلى 30. يعطي البوت تلميحات أعلى أو أقل.",
        "начинает угадывание числа от 1 до 30. Бот подсказывает, больше оно или меньше."),
    "shows your points for the current session.": ("يعرض نقاطك في الجلسة الحالية.", "показывает очки за текущую сессию."),
    "Try": ("جرّب", "Попробуйте"),
    "or": ("أو", "или"),
    ", or": ("، أو", ", или"),
    "for a quick room prompt.": ("لتنشيط الدردشة.", "чтобы оживить чат."),
    "Use": ("استخدم", "Используйте"),
    "for a reply. Chat replies support English and Egyptian Arabic, with availability depending on the room and service setup.": (
        "لطلب رد. الردود بالإنجليزية والعربية المصرية حسب إعدادات الغرفة والخدمة.",
        "для ответа. Поддержка английского и египетского арабского зависит от настроек комнаты и сервиса."),
    "asks the bot to move its avatar to a free seat near you, when the furniture has a suitable seat.": (
        "يطلب من البوت الانتقال إلى مقعد فارغ قريب منك إذا توفر.",
        "просит бота пересесть на свободное место рядом с вами, если такое есть."),
    "Send": ("أرسل", "Отправьте"),
    "in a new one-to-one IMVU message to the bot for an invitation to a room it has joined. IMVU applies the room’s admission and capacity rules.": (
        "في رسالة خاصة جديدة للبوت على IMVU لتلقي دعوة إلى غرفة انضم إليها. تطبّق IMVU قواعد الدخول والسعة.",
        "в новом личном сообщении боту в IMVU, чтобы получить приглашение в комнату, к которой он подключён. IMVU учитывает правила входа и вместимость."),
    "and": ("و", "и"),
    "in a welcome of up to 300 characters. Turn on Quiet mode to limit conversational follow-ups while keeping direct requests and commands.": (
        "في ترحيب يصل إلى 300 حرف. فعّل الوضع الهادئ لتقليل المتابعات مع استمرار الردود المباشرة والأوامر.",
        "в приветствии до 300 символов. Тихий режим ограничивает продолжение бесед, сохраняя ответы на прямые запросы и команды."),
    "Website room admins can change settings only in assigned rooms. The service owner approves requests, connects or disconnects the bot, and manages admin grants.": (
        "يعدّل مشرفو الموقع إعدادات الغرف المعيّنة لهم فقط. يوافق مالك الخدمة على الطلبات ويوصل البوت ويفصله ويدير صلاحيات المشرفين.",
        "Администраторы сайта меняют настройки только назначенных комнат. Владелец сервиса одобряет заявки, подключает и отключает бота и управляет правами."),
    "Get your room’s MP3 link with": ("احصل على رابط MP3 للغرفة باستخدام", "Получите MP3-ссылку комнаты командой"),
    ". Guests use": (". يستخدم الزوار", ". Гости используют"),
    "to request tracks and": ("لطلب الأغاني و", "для запроса треков и"),
    "to see what’s next.": ("لعرض الأغاني التالية.", "чтобы посмотреть очередь."),
    "Write a greeting using": ("اكتب ترحيبًا باستخدام", "Напишите приветствие с"),
    "for the visitor and": ("للزائر و", "для гостя и"),
    "for the room. Guests can start trivia with": (
        "للغرفة. يمكن للزوار بدء مسابقة باستخدام", "для комнаты. Гости могут начать викторину командой"),
    "or a number round with": ("أو جولة تخمين باستخدام", "или угадывание числа командой"),
}


def translate(text, language):
    if language == "en":
        return text
    pair = TEXT.get(text)
    if pair:
        return pair[language == "ru"]
    if re.fullmatch(r"Room [1-9]\d*-\d+", text):
        return ("الغرفة " if language == "ar" else "Комната ") + text[5:]
    if text.startswith("Customer · "):
        return ("العميل · " if language == "ar" else "Клиент · ") + text[11:]
    return text


def translate_html(markup, language):
    if language == "en":
        return markup

    def replace(value):
        plain = unescape(value.strip())
        localized = translate(plain, language)
        if localized == plain:
            return value
        start = len(value) - len(value.lstrip())
        end = len(value.rstrip())
        return value[:start] + escape(localized) + value[end:]

    # Keep commands and rendered greetings exactly as supplied.
    chunks = re.split(r'(<(?:blockquote|code|bdi)\b[^>]*>.*?</(?:blockquote|code|bdi)>|<[^>]+>)', markup, flags=re.S)
    return "".join(chunk if index % 2 else replace(chunk) for index, chunk in enumerate(chunks))


@web.middleware
async def website_locale(request, handler):
    language = request.cookies.get("ramzy_site_language", "en")
    token = locale.set((language if language in LANGUAGES else "en", request.path))
    try:
        return await handler(request)
    finally:
        locale.reset(token)


async def set_website_locale(request):
    language = request.match_info.get("language") or request.query.get("language")
    if language not in LANGUAGES:
        raise web.HTTPNotFound()
    target = request.query.get("next", "/")
    response = web.HTTPSeeOther(target if target in RETURN_PATHS else "/")
    response.set_cookie("ramzy_site_language", language, max_age=365 * 86400, secure=True,
                        httponly=True, samesite="Lax", path="/")
    raise response
