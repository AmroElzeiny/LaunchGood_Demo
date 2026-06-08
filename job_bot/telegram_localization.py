from __future__ import annotations

from typing import Final

from job_bot.project_filters import (
    project_filter_example as shared_project_filter_example,
    project_filter_label as shared_project_filter_label,
)


SUPPORTED_UI_LANGUAGES: Final[tuple[str, str]] = ("en", "ru")
DEFAULT_UI_LANGUAGE: Final[str] = "en"
BRAND_NAME: Final[str] = "ZapLance"
CANONICAL_ROLE_TITLE: Final[str] = "UX/UI Designer"


def normalize_ui_language(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in SUPPORTED_UI_LANGUAGES else ""


def ui_language_or_default(value: object) -> str:
    normalized = normalize_ui_language(value)
    return normalized or DEFAULT_UI_LANGUAGE


UI_STRINGS: Final[dict[str, dict[str, str]]] = {
    "language_gate_title": {
        "en": "Choose your language",
        "ru": "Выберите язык",
    },
    "language_gate_text": {
        "en": "Pick the Telegram UI language to continue.",
        "ru": "Сначала выберите язык интерфейса в Telegram.",
    },
    "language_saved_en": {
        "en": "English is now active.",
        "ru": "Язык интерфейса переключён на английский.",
    },
    "language_saved_ru": {
        "en": "Russian is now active.",
        "ru": "Язык интерфейса переключён на русский.",
    },
    "language_label_en": {
        "en": "English",
        "ru": "English",
    },
    "language_label_ru": {
        "en": "Русский",
        "ru": "Русский",
    },
    "ai_processing_notice": {
        "en": "Hold on while the system understands your input.",
        "ru": "Секунду, я разбираю ваш ответ.",
    },
    "welcome_text": {
        "en": (
            "Welcome to ZapLance\n"
            "Fast freelance project alerts for UX/UI work.\n"
            "Set your filters once and ZapLance will monitor matching sources for you."
        ),
        "ru": (
            "Добро пожаловать в ZapLance\n"
            "Быстрые уведомления о фриланс-проектах для UX/UI.\n"
            "Настройте поиск один раз, и бот сам будет отслеживать подходящие источники."
        ),
    },
    "about_text": {
        "en": (
            "ℹ️ About ZapLance\n"
            "⚡️ ZapLance helps UX/UI freelancers discover fresh freelance projects, briefs, and contract work fast.\n\n"
            "🎯 Set your specialty, project filters, sources, and keywords once, and ZapLance will continuously monitor matching project feeds for you.\n\n"
            "⚡️ Why this matters\n"
            "Many freelancers discover strong projects too late.\n"
            "ZapLance helps you spot relevant posts earlier, so you can reach out while the project is still fresh.\n\n"
            "📲 When a relevant project appears, ZapLance sends it to your Telegram with:\n"
            "💼 Project title\n"
            "👤 Client / company / requester\n"
            "📍 Location or remote scope\n"
            "💰 Budget or hourly rate when available\n"
            "🤖 AI summary\n"
            "🔗 Apply / contact link\n\n"
            "✨ Why freelancers choose ZapLance\n"
            "⚡️ Fast project alerts\n"
            "🔎 Less manual searching\n"
            "🎯 More relevant results with your filters\n"
            "🌐 Multiple sources in one stream\n\n"
            "🚀 Reach out earlier. Save time on searching. Win better freelance projects faster."
        ),
        "ru": (
            "ℹ️ О ZapLance\n"
            "⚡️ ZapLance помогает находить новые фриланс-проекты, брифы и контрактные проекты менее чем за 30 секунд после публикации.\n\n"
            "🎯 Один раз настройте свою основную специализацию, фильтры проектов, источники и ключевые слова, и мы будем постоянно отслеживать для вас подходящие возможности.\n\n"
            "⚡️ Почему это важно\n"
            "Большинство фрилансеров узнают о новых брифах и контрактах только через несколько часов.\n"
            "С ZapLance вы можете видеть подходящие проекты уже через секунды после публикации.\n\n"
            "📲 Как только появляется релевантная возможность, ZapLance сразу отправляет её в ваш Telegram с такими данными:\n"
            "💼 Название проекта или возможности\n"
            "👥 Клиент / компания / заказчик\n"
            "📍 Местоположение\n"
            "💰 Бюджет / ставка / оплата, если указаны\n"
            "🤖 Краткое резюме от ИИ\n"
            "🔗 Ссылку для отклика или связи\n\n"
            "✨ Почему пользователи выбирают ZapLance\n"
            "⚡️ Уведомления меньше чем за 1 минуту\n"
            "🚀 Более быстрый доступ к свежим возможностям\n"
            "🔎 Меньше ручного поиска\n"
            "🎯 Лучшая точность благодаря гибким фильтрам\n"
            "🌐 Несколько источников в одном потоке\n\n"
            "🚀 Откликайтесь раньше. Отправляйте предложения быстрее. Получайте проекты быстрее."
        ),
    },
    "support_text": {
        "en": (
            "Support\n"
            "Need help, want to report an issue, or want us to review a source?\n"
            "Use the support button below."
        ),
        "ru": (
            "Поддержка\n"
            "Если нужна помощь, хотите сообщить о проблеме или попросить проверить источник, напишите нам.\n"
            "Кнопка ниже откроет поддержку."
        ),
    },
    "source_removed_notice": {
        "en": (
            "⚠️ Source Update\n"
            "{source_name} was removed from ZapLance for maintenance and is no longer available right now.\n"
            "It has already been removed from your selected websites.\n\n"
            "Current sources:\n"
            "{current_sources}\n\n"
            "Please choose replacement websites or add your own custom website."
        ),
        "ru": (
            "⚠️ Обновление источников\n"
            "{source_name} временно удалён из ZapLance на техническое обслуживание и сейчас недоступен.\n"
            "Мы уже убрали его из вашего списка выбранных сайтов.\n\n"
            "Текущие источники:\n"
            "{current_sources}\n\n"
            "Пожалуйста, выберите другие сайты или добавьте свой собственный источник."
        ),
    },
    "source_removed_notice_empty": {
        "en": (
            "⚠️ Source Update\n"
            "{source_name} was removed from ZapLance for maintenance and is no longer available right now.\n"
            "It has already been removed from your selected websites.\n\n"
            "You do not have any selected sources left right now.\n"
            "Please choose other websites or add your own custom website so alerts can keep working."
        ),
        "ru": (
            "⚠️ Обновление источников\n"
            "{source_name} временно удалён из ZapLance на техническое обслуживание и сейчас недоступен.\n"
            "Мы уже убрали его из вашего списка выбранных сайтов.\n\n"
            "Сейчас у вас не осталось выбранных источников.\n"
            "Пожалуйста, выберите другие сайты или добавьте свой собственный источник, чтобы уведомления продолжали работать."
        ),
    },
    "subscription_text": {
        "en": (
            "Subscription Plans\n"
            "Choose the plan that fits your freelance search:\n"
            "• 14-Day Plan - $4.99\n"
            "• Monthly - $7.49\n"
            "• Quarterly - $14.49\n"
            "• Free trial - 7 days\n"
            "• Renewal is manual."
        ),
        "ru": (
            "Планы подписки\n"
            "Выберите план для поиска проектов:\n"
            "• План на 14 дней - $4.99\n"
            "• 1 месяц - $7.49\n"
            "• 3 месяца - $14.49\n"
            "• Бесплатный пробный период - 7 дней\n"
            "• Продление только вручную."
        ),
    },
    "main_menu_text": {
        "en": (
            "🏠 Main Menu\n"
            "⚡️ Welcome to ZapLance ⚡️\n"
            "🚀 We deliver relevant freelance opportunities, projects, and contracts from multiple sources directly to your Telegram in under 1 minute from posting time.\n\n"
            "🎯 Set your specialty, project filters, sources, and keywords once, and we'll monitor the market for you.\n\n"
            "⚡️ Why this matters\n"
            "Most freelancers discover new briefs and contracts hours later.\n"
            "With ZapLance, you can see relevant opportunities within seconds after they are published.\n\n"
            "🛠️ Use the menu below to manage your alerts, update your filters, or upgrade your plan.\n\n"
            "{alert_line}\n"
            "{subscription_line}"
        ),
        "ru": (
            "🏠 Главное меню\n"
            "⚡️ Добро пожаловать в ZapLance ⚡️\n"
            "🚀 Мы отправляем подходящие фриланс-проекты, заказы и контрактные возможности из разных источников прямо в ваш Telegram менее чем за 1 минуту после публикации.\n\n"
            "🎯 Один раз настройте свою специализацию, фильтры проектов, источники и ключевые слова, и мы будем отслеживать рынок за вас.\n\n"
            "⚡️ Почему это важно\n"
            "Большинство фрилансеров узнают о новых брифах и контрактах только через несколько часов.\n"
            "С ZapLance вы можете видеть подходящие возможности уже через секунды после публикации.\n\n"
            "🛠️ Используйте меню ниже, чтобы управлять уведомлениями, обновлять фильтры или изменить свой тариф.\n\n"
            "{alert_line}\n"
            "{subscription_line}"
        ),
    },
    "alert_not_set": {
        "en": "🔔 Project alerts: not set up yet",
        "ru": "🔔 Уведомления о проектах: ещё не настроены",
    },
    "alert_ready": {
        "en": "🔔 Project alerts: ready to activate",
        "ru": "🔔 Уведомления о проектах: готовы к запуску",
    },
    "alert_paused": {
        "en": "🔔 Project alerts: paused",
        "ru": "🔔 Уведомления о проектах: на паузе",
    },
    "alert_active": {
        "en": "🔔 Project alerts: active",
        "ru": "🔔 Уведомления о проектах: активны",
    },
    "subscription_inactive": {
        "en": "💳 Subscription: inactive",
        "ru": "💳 Подписка: неактивна",
    },
    "role_prompt_create": {
        "en": (
            "Step 1 of 4\n"
            "Primary specialty\n"
            "ZapLance is currently focused on one specialty only.\n"
            "Tap UX/UI Designer to continue."
        ),
        "ru": (
            "Шаг 1 из 4\n"
            "Специализация\n"
            "Сейчас ZapLance работает только с одной специализацией.\n"
            "Нажмите UX/UI Designer, чтобы продолжить."
        ),
    },
    "role_prompt_edit": {
        "en": (
            "Primary specialty\n"
            "ZapLance is currently focused on one specialty only.\n"
            "Tap UX/UI Designer to keep this alert aligned."
        ),
        "ru": (
            "Специализация\n"
            "Сейчас ZapLance работает только с одной специализацией.\n"
            "Нажмите UX/UI Designer, чтобы поиск оставался в нужной специализации."
        ),
    },
    "location_prompt_create_heading": {
        "en": "Location filters were removed",
        "ru": "Фильтры по местоположению удалены",
    },
    "location_prompt_edit_heading": {
        "en": "Location",
        "ru": "Страна и формат работы",
    },
    "location_prompt_body": {
        "en": (
            "Choose one or more location filters.\n"
            "Remote Global accepts fully global remote work.\n"
            "Remote within a country lets you save a single country for remote work.\n"
            "On-site/hybrid within a country lets you save one country for non-remote or hybrid work.\n"
            "Custom Location lets you type a specific city, state, country, or multiple locations separated with ; or &.\n"
            "Tap Save Selections when you're done.\n"
            "Current selections:\n{current_location}"
        ),
        "ru": (
            "Выберите один или несколько вариантов по месту и формату работы.\n"
            "«Удалённо из любой страны» подходит для полностью удалённых проектов без ограничений по стране.\n"
            "«Удалённо в выбранной стране» сохраняет одну страну для удалённой работы.\n"
            "«Офис / гибрид в выбранной стране» сохраняет одну страну для офиса или гибрида.\n"
            "«Указать своё местоположение» позволяет ввести город, регион, страну или несколько вариантов через ; или &.\n"
            "Нажмите «Сохранить», когда закончите.\n"
            "Текущий выбор:\n{current_location}"
        ),
    },
    "location_remote_country_prompt": {
        "en": "Enter the country where you want remote projects. Example: Egypt, Germany, or United States.",
        "ru": "Введите страну, в которой хотите получать удалённые проекты. Например: Египет, Германия или США.",
    },
    "location_onsite_country_prompt": {
        "en": "Enter the country where you want on-site or hybrid projects. Example: Egypt, Germany, or United States.",
        "ru": "Введите страну, в которой хотите получать офисные или гибридные проекты. Например: Египет, Германия или США.",
    },
    "location_custom_prompt": {
        "en": (
            "Enter the location you want us to consider.\n"
            "Include the country and state or province if you know them.\n"
            "Example: USA, California or Egypt, Cairo."
        ),
        "ru": (
            "Введите местоположение, которое нужно учитывать.\n"
            "По возможности укажите страну и регион.\n"
            "Например: США, Калифорния или Египет, Каир."
        ),
    },
    "location_confirm_remote_country": {
        "en": (
            "Here is what I understood for your remote-country filter:\n"
            "Country: {country}\n"
            "Detected value: {canonical}\n\n"
            "Is this correct?"
        ),
        "ru": (
            "Вот что я понял для вашего фильтра по стране для удаленной работы:\n"
            "Страна: {country}\n"
            "Распознанное значение: {canonical}\n\n"
            "Все верно?"
        ),
    },
    "location_confirm_onsite_country": {
        "en": (
            "Here is what I understood for your on-site/hybrid country filter:\n"
            "Country: {country}\n"
            "Detected value: {canonical}\n\n"
            "Is this correct?"
        ),
        "ru": (
            "Вот что я понял для вашего фильтра по стране для офиса/гибрида:\n"
            "Страна: {country}\n"
            "Распознанное значение: {canonical}\n\n"
            "Все верно?"
        ),
    },
    "location_confirm_custom": {
        "en": (
            "Here is what I understood from your location:\n"
            "Country: {country}\n"
            "State/Province: {state}\n"
            "City: {city}\n"
            "Detected location: {canonical}\n\n"
            "Is this correct?"
        ),
        "ru": (
            "Вот что я понял по вашему местоположению:\n"
            "Страна: {country}\n"
            "Регион: {state}\n"
            "Город: {city}\n"
            "Распознанное местоположение: {canonical}\n\n"
            "Все верно?"
        ),
    },
    "location_confirm_multi": {
        "en": (
            "Here is what I understood from your location list:\n"
            "{items}\n\n"
            "This will match if any one of these locations matches.\n\n"
            "Is this correct?"
        ),
        "ru": (
            "Вот что я понял из вашего списка мест:\n"
            "{items}\n\n"
            "Фильтр сработает, если подойдет хотя бы один вариант из списка.\n\n"
            "Все верно?"
        ),
    },
    "project_filters_prompt_create_heading": {
        "en": "Step 2 of 4",
        "ru": "Шаг 2 из 4",
    },
    "project_filters_prompt_edit_heading": {
        "en": "Project Filters",
        "ru": "Фильтры проектов",
    },
    "project_filters_prompt_body": {
        "en": (
            "Choose a project filter to edit.\n"
            "You can set deliverables plus min/max payment separately in USD and RUB.\n"
            "Send only one value at a time and the system will store it in the right field.\n\n"
            "{summary}"
        ),
        "ru": (
            "Выберите фильтр проекта.\n"
            "Можно отдельно задать задачи проекта и мин./макс. оплату в USD и RUB.\n"
            "Отправляйте по одному значению за раз, и я сохраню его в нужное поле.\n\n"
            "{summary}"
        ),
    },
    "project_filters_empty": {
        "en": "Current project filters: none yet.",
        "ru": "Фильтры проектов пока не заданы.",
    },
    "project_filter_value_prompt": {
        "en": (
            "Send the value for {field_label}.\n"
            "Example: {example}\n"
            "Send `clear` to remove this filter.\n"
            "Current: {current_value}"
        ),
        "ru": (
            "Введите значение для поля {field_label}.\n"
            "Пример: {example}\n"
            "Чтобы убрать фильтр, отправьте `очистить`.\n"
            "Сейчас: {current_value}"
        ),
    },
    "project_filter_saved": {
        "en": "{field_label} was updated.",
        "ru": "{field_label}: сохранено.",
    },
    "project_filter_cleared": {
        "en": "{field_label} was cleared.",
        "ru": "{field_label}: очищено.",
    },
    "website_prompt_create_heading": {
        "en": "Step 3 of 4",
        "ru": "Шаг 3 из 4",
    },
    "website_prompt_edit_heading": {
        "en": "Sources",
        "ru": "Источники",
    },
    "website_prompt_body": {
        "en": (
            "Choose the sources you want us to monitor.\n"
            "Built-in sites already know whether to use USD [crypto] or RUB [crypto].\n"
            "For a custom website, you will choose the currency after sending the link.\n"
            "Selected sources:\n{selected_sources}"
        ),
        "ru": (
            "Выберите источники, которые нужно отслеживать.\n"
            "Для встроенных сайтов валюта уже задана: USD [крипто] или RUB [крипто].\n"
            "Для своего сайта вы выберете валюту после отправки ссылки.\n"
            "Выбранные источники:\n{selected_sources}"
        ),
    },
    "website_choose_one": {
        "en": "Choose at least one source before continuing.",
        "ru": "Перед продолжением выберите хотя бы один источник.",
    },
    "website_invalid": {
        "en": "That link is invalid. Send a full URL starting with http:// or https://.",
        "ru": "Ссылка недействительна. Отправьте полный URL, начинающийся с http:// или https://.",
    },
    "website_unmonitorable": {
        "en": (
            "This does not look like a monitorable feed, board, or refreshable listing page.\n"
            "Reason: {reason}"
        ),
        "ru": (
            "Это не похоже на отслеживаемую ленту проектов, доску проектов или обновляемую страницу со списком.\n"
            "Причина: {reason}"
        ),
    },
    "website_added": {
        "en": "New source added successfully.",
        "ru": "Новый источник успешно добавлен.",
    },
    "website_custom_prompt": {
        "en": (
            "🌐 Send the link of a freelance projects search-results page.\n"
            "It must be a page with result cards that update over time so the bot can pull new posts.\n"
            "Please avoid homepages and single-job pages."
        ),
        "ru": (
            "🌐 Отправьте ссылку на страницу результатов поиска фриланс-проектов.\n"
            "Это должна быть страница с карточками результатов, которая со временем обновляется, чтобы бот мог находить новые публикации.\n"
            "Пожалуйста, не отправляйте главные страницы сайтов и страницы одного проекта."
        ),
    },
    "website_currency_prompt": {
        "en": "Choose the working currency for this custom website:\n{website_url}",
        "ru": "Выберите рабочую валюту для этого сайта:\n{website_url}",
    },
    "keywords_prompt_create_heading": {
        "en": "Step 4 of 4",
        "ru": "Шаг 4 из 4",
    },
    "keywords_prompt_edit_heading": {
        "en": "Keywords",
        "ru": "Ключевые слова",
    },
    "keywords_prompt_body": {
        "en": (
            "Add keywords to improve relevance.\n"
            "Use skills, deliverables, industries, tools, or project types you want tracked.\n"
            "Examples: landing page, dashboard, design system, mobile app redesign, Figma, SaaS, fintech\n"
            "Current: {current_keywords}"
        ),
        "ru": (
            "Добавьте ключевые слова, чтобы точнее отбирать проекты.\n"
            "Можно указывать навыки, задачи, инструменты, ниши или типы проектов.\n"
            "Примеры: лендинг, дашборд, дизайн-система, редизайн мобильного приложения, Figma, SaaS, финтех\n"
            "Сейчас: {current_keywords}"
        ),
    },
    "keywords_input_prompt": {
        "en": "Send keywords separated by commas.",
        "ru": "Отправьте ключевые слова через запятую.",
    },
    "keywords_reenter_prompt": {
        "en": "Send your keywords again.",
        "ru": "Введите ключевые слова ещё раз.",
    },
    "keywords_invalid": {
        "en": "I did not detect any valid keywords. Send comma-separated keywords or tap Skip.",
        "ru": "Не вижу корректных ключевых слов. Отправьте их через запятую или нажмите «Пропустить».",
    },
    "keywords_confirmation_intro": {
        "en": "Here is what the AI understood from your keywords:",
        "ru": "Вот как я понял ваши ключевые слова:",
    },
    "alert_summary_title": {
        "en": "Your alert is ready",
        "ru": "Поиск настроен",
    },
    "my_alert_title": {
        "en": "My Alert",
        "ru": "Мой поиск",
    },
    "edit_alert_text": {
        "en": "Choose what you want to edit.",
        "ru": "Выберите, что хотите изменить.",
    },
    "empty_alert": {
        "en": "You do not have an alert yet.",
        "ru": "У вас пока нет настроенного поиска.",
    },
    "alert_deleted": {
        "en": "Your alert has been deleted.",
        "ru": "Поиск удалён.",
    },
    "edit_saved": {
        "en": "Your alert settings have been updated.",
        "ru": "Настройки поиска обновлены.",
    },
    "activate_trial_prompt": {
        "en": "Activate your 7-day free trial to start receiving matches.",
        "ru": "Активируйте бесплатный пробный период на 7 дней, чтобы начать получать подходящие проекты.",
    },
    "alert_ready_active": {
        "en": (
            "Your alert is ready.\n"
            "ZapLance is now monitoring your selected sources.\n"
            "Matches will arrive when something relevant appears."
        ),
        "ru": (
            "Поиск настроен.\n"
            "ZapLance уже отслеживает выбранные источники.\n"
            "Как только появится подходящий проект, я сразу пришлю его."
        ),
    },
    "alert_paused_message": {
        "en": "Your alert is currently paused.",
        "ru": "Поиск сейчас на паузе.",
    },
    "alert_resumed_message": {
        "en": "Your alert is active again.",
        "ru": "Поиск снова активен.",
    },
    "access_active_with_alert": {
        "en": "Your access is already active.\nZapLance is already monitoring your alert.",
        "ru": "Доступ уже активен.\nZapLance уже отслеживает ваш поиск.",
    },
    "access_active_without_alert": {
        "en": "Your access is already active.\nYou can create your alert whenever you're ready.",
        "ru": "Доступ уже активен.\nВы можете настроить поиск в любой момент.",
    },
    "trial_used": {
        "en": "Your 7-day free trial was already used.\nChoose a subscription plan to keep receiving matches.",
        "ru": "Ваш пробный период на 7 дней уже использован.\nВыберите платный план, чтобы продолжать получать подходящие проекты.",
    },
    "trial_active_with_alert": {
        "en": (
            "Your 7-day free trial is now active.\n"
            "ZapLance is now monitoring your selected sources.\n"
            "You will receive a notification as soon as a matching project appears."
        ),
        "ru": (
            "Ваш пробный период на 7 дней активирован.\n"
            "ZapLance уже отслеживает выбранные источники.\n"
            "Вы получите уведомление, как только появится подходящий проект."
        ),
    },
    "trial_active_without_alert": {
        "en": "Your 7-day free trial is now active.\nNext step: let’s set up your alert.",
        "ru": "Ваш пробный период на 7 дней активирован.\nСледующий шаг: давайте настроим поиск.",
    },
    "payment_link_error": {
        "en": "I could not create a payment link right now.\nError: {error}",
        "ru": "Не удалось создать ссылку на оплату.\nОшибка: {error}",
    },
    "payment_session_not_found": {
        "en": "Payment session not found.",
        "ru": "Не удалось найти этот платёж.",
    },
    "expiry_trial": {
        "en": "Your free trial ends soon. Choose a plan to continue receiving matches.",
        "ru": "Ваш пробный период скоро закончится. Выберите план, чтобы продолжать получать подходящие проекты.",
    },
    "expiry_subscription": {
        "en": "Your subscription will expire soon. Renew to continue receiving matches.",
        "ru": "Срок подписки скоро закончится. Продлите её, чтобы продолжать получать подходящие проекты.",
    },
    "expired_trial": {
        "en": "Your free trial has ended. Your alert is paused.",
        "ru": "Ваш пробный период закончился. Поиск поставлен на паузу.",
    },
    "expired_subscription": {
        "en": "Your subscription has expired. Your alert is inactive.",
        "ru": "Срок подписки истёк. Поиск неактивен.",
    },
    "use_menu_buttons": {
        "en": "Use the menu buttons below so I can keep the flow organized.",
        "ru": "Пожалуйста, используйте кнопки ниже, чтобы настройка не сбилась.",
    },
    "support_contact_message": {
        "en": "Please contact support using the button below.",
        "ru": "Пожалуйста, свяжитесь с поддержкой через кнопку ниже.",
    },
    "feedback_prompt": {
        "en": (
            "Tell me why this match was not relevant.\n"
            "You can mention the title, scope, location, budget, remote scope, seniority, industry, or anything else."
        ),
        "ru": (
            "Напишите, почему этот проект вам не подошёл.\n"
            "Можно указать задачи, местоположение, бюджет, формат, уровень, нишу или любую другую причину."
        ),
    },
    "feedback_saved": {
        "en": "Feedback saved. I will use it to avoid similar mismatches next time.",
        "ru": "Спасибо, учту это в следующих рекомендациях.",
    },
    "alert_health_missing": {
        "en": (
            "Why you are not receiving matches\n"
            "I need an active alert and at least one monitored source before I can build this report."
        ),
        "ru": (
            "Почему мне пока не приходят подходящие проекты\n"
            "Чтобы собрать такой отчёт, мне нужен активный поиск и хотя бы один отслеживаемый источник."
        ),
    },
    "payment_refresh_error": {
        "en": "Could not refresh the payment status right now.\nError: {error}",
        "ru": "Не удалось обновить статус платежа.\nОшибка: {error}",
    },
    "payment_status_final": {
        "en": "Payment status: {status}\nYou can start a new payment from the Subscription menu.",
        "ru": "Статус платежа: {status}\nЕсли нужно, можно создать новый платёж в меню подписки.",
    },
    "payment_status_update": {
        "en": "Payment status: {status}\nLink: {payment_url}",
        "ru": "Статус платежа: {status}\nСсылка: {payment_url}",
    },
    "payment_status_changed": {
        "en": "Payment status changed to: {status}",
        "ru": "Новый статус платежа: {status}",
    },
    "payment_status_background_update": {
        "en": "Payment status update: {status}\nLink: {payment_url}",
        "ru": "Статус платежа обновился: {status}\nСсылка: {payment_url}",
    },
    "payment_cancelled": {
        "en": "Payment canceled. Status is marked as failed.",
        "ru": "Платёж отменён.",
    },
    "payment_checkout_message": {
        "en": (
            "Payment for {plan_title}\n"
            "Duration: {duration_days} days\n"
            "Price: ${amount_usd:.2f}\n"
            "Renewal: manual\n\n"
            "Your payment link is ready:\n"
            "{payment_url}\n"
            "Status: {status}"
        ),
        "ru": (
            "Оплата плана {plan_title}\n"
            "Срок: {duration_days} дней\n"
            "Цена: ${amount_usd:.2f}\n"
            "Продление: вручную\n\n"
            "Ссылка на оплату готова:\n"
            "{payment_url}\n"
            "Статус: {status}"
        ),
    },
    "payment_success_message": {
        "en": (
            "Payment successful!\n"
            "Your subscription is now active.\n"
            "Plan: {plan_name}\n"
            "Expires: {expires_at}{next_step}"
        ),
        "ru": (
            "Платёж прошёл успешно.\n"
            "Подписка активна.\n"
            "План: {plan_name}\n"
            "Действует до: {expires_at}{next_step}"
        ),
    },
    "payment_success_next_step": {
        "en": "\n\nNext step: let's set up your alert so we can start sending matches.",
        "ru": "\n\nСледующий шаг: давайте настроим поиск, чтобы начать присылать подходящие проекты.",
    },
}


BUTTON_STRINGS: Final[dict[str, dict[str, str]]] = {
    "create_alert": {"en": "🚀 Create Alert", "ru": "🚀 Настроить поиск"},
    "my_alert": {"en": "📌 My Alert", "ru": "📌 Мой поиск"},
    "view_my_alert": {"en": "📌 View My Alert", "ru": "📌 Открыть поиск"},
    "subscription": {"en": "💳 Subscription", "ru": "💳 Подписка"},
    "subscribe": {"en": "💳 Subscribe", "ru": "💳 Подписаться"},
    "about": {"en": "ℹ️ About ZapLance", "ru": "ℹ️ О ZapLance"},
    "support": {"en": "🛟 Support", "ru": "🛟 Поддержка"},
    "language": {"en": "🌐 Language", "ru": "🌐 Язык"},
    "primary_specialty": {"en": "🎯 Primary Specialty", "ru": "🎯 Специализация"},
    "location": {"en": "📍 Location", "ru": "📍 Страна и формат"},
    "project_filters": {"en": "🧩 Project Filters", "ru": "🧩 Фильтры проектов"},
    "sources": {"en": "🌐 Sources", "ru": "🌐 Источники"},
    "keywords": {"en": "🏷️ Keywords", "ru": "🏷️ Ключевые слова"},
    "back": {"en": "⬅️ Back", "ru": "⬅️ Назад"},
    "back_main": {"en": "⬅️ Main Menu", "ru": "⬅️ Главное меню"},
    "continue": {"en": "Continue", "ru": "Продолжить"},
    "done": {"en": "Done", "ru": "Готово"},
    "skip": {"en": "Skip", "ru": "Пропустить"},
    "confirm": {"en": "Confirm", "ru": "Подтвердить"},
    "reenter": {"en": "Re-enter", "ru": "Ввести заново"},
    "save_selections": {"en": "✅ Save Selections", "ru": "✅ Сохранить"},
    "clear_selections": {"en": "🧹 Clear Selections", "ru": "🧹 Очистить"},
    "type_custom_location": {"en": "Type Custom Location", "ru": "Указать своё местоположение"},
    "select_all": {"en": "✅ Select/Unselect All", "ru": "✅ Выбрать всё / снять всё"},
    "add_custom_website": {"en": "🔗 Add Custom Website", "ru": "🔗 Добавить свой сайт"},
    "website_currency_usd": {"en": "💵 USD [crypto]", "ru": "💵 USD [крипто]"},
    "website_currency_rub": {"en": "💴 Rubles [crypto]", "ru": "💴 Рубли [крипто]"},
    "add_keywords": {"en": "🏷️ Add Keywords", "ru": "🏷️ Добавить ключевые слова"},
    "confirm_keywords": {"en": "🏷️ Confirm Keywords", "ru": "🏷️ Сохранить ключевые слова"},
    "reenter_keywords": {"en": "🏷️ Re-enter Keywords", "ru": "🏷️ Ввести заново"},
    "activate_alert": {"en": "🚀 Activate Alert", "ru": "🚀 Запустить поиск"},
    "edit_alert": {"en": "⚙️ Edit Alert", "ru": "⚙️ Изменить поиск"},
    "pause_alert": {"en": "⏸️ Pause Alert", "ru": "⏸️ Поставить на паузу"},
    "resume_alert": {"en": "▶️ Resume Alert", "ru": "▶️ Возобновить"},
    "delete_alert": {"en": "🗑️ Delete Alert", "ru": "🗑️ Удалить поиск"},
    "undo": {"en": "Undo", "ru": "Отменить"},
    "why_no_matches": {"en": "Why am I not receiving matches?", "ru": "Почему мне пока не приходят подходящие проекты?"},
    "just_continue": {"en": "Keep Current Setup", "ru": "Оставить как есть"},
    "activate_trial": {"en": "💳 Activate Free Trial", "ru": "💳 Включить пробный период"},
    "claim_trial": {"en": "💳 Claim Free Trial", "ru": "💳 Забрать пробный период"},
    "require_visible_budget": {"en": "Require Visible Budget", "ru": "Только с указанным бюджетом"},
    "allow_hidden_budget": {"en": "Allow Hidden Budget", "ru": "Можно без бюджета"},
    "contact_support": {"en": "🛟 Contact Support", "ru": "🛟 Написать в поддержку"},
    "remote_global": {"en": "Remote Global", "ru": "Удалённо из любой страны"},
    "remote_country": {"en": "Remote within a country", "ru": "Удалённо в выбранной стране"},
    "onsite_country": {"en": "On-site/hybrid within a country", "ru": "Офис / гибрид в выбранной стране"},
    "english": {"en": "🌐 English", "ru": "🌐 English"},
    "russian": {"en": "🌐 Русский", "ru": "🌐 Русский"},
    "plan_14_day": {"en": "⏳ 14-Day Plan - $4.99", "ru": "⏳ 14 дней - $4.99"},
    "plan_monthly": {"en": "📅 Monthly - $7.49", "ru": "📅 1 месяц - $7.49"},
    "plan_quarterly": {"en": "🚀 Quarterly - $14.49", "ru": "🚀 3 месяца - $14.49"},
    "payment_update": {"en": "🔄 Update", "ru": "🔄 Проверить статус"},
    "payment_cancel": {"en": "❌ Cancel Payment", "ru": "❌ Отменить платёж"},
}


MESSAGE_STRINGS: Final[dict[str, dict[str, str]]] = {
    "digest_header": {
        "en": "📬 ZapLance Digest\n✨ You have {count} new matches waiting.",
        "ru": "📬 Дайджест ZapLance\n✨ У вас {count} новых подходящих проектов.",
        "ar": "ملخص ZapLance\nلديك {count} تطابقات جديدة بانتظارك.",
    },
    "card_new_project_match": {
        "en": "✨ New Project Match",
        "ru": "✨ Новый подходящий проект",
        "ar": "مشروع مطابق جديد",
    },
    "card_project": {"en": "💼 Project", "ru": "💼 Проект", "ar": "المشروع"},
    "card_client": {"en": "👤 Client", "ru": "👤 Клиент / заказчик", "ar": "العميل"},
    "card_unknown": {"en": "Unknown", "ru": "Не указано", "ar": "غير محدد"},
    "card_budget_rate": {"en": "💰 Budget / Rate", "ru": "💰 Бюджет / ставка", "ar": "الميزانية / السعر"},
    "card_engagement_type": {"en": "🧩 Engagement Type", "ru": "🧩 Формат сотрудничества", "ar": "نوع التعاون"},
    "card_timeline_duration": {"en": "⏳ Timeline / Duration", "ru": "⏳ Срок проекта", "ar": "المدة / الجدول الزمني"},
    "card_skills_requested": {"en": "🛠️ Skills Requested", "ru": "🛠️ Навыки и требования", "ar": "المهارات المطلوبة"},
    "card_remote_location": {"en": "📍 Remote / Location", "ru": "📍 Формат / местоположение", "ar": "عن بُعد / الموقع"},
    "card_source": {"en": "🌐 Source", "ru": "🌐 Источник", "ar": "المصدر"},
    "card_scope_summary": {"en": "📝 Scope Summary:", "ru": "📝 Что нужно сделать:", "ar": "ملخص نطاق العمل:"},
    "card_why_matched": {"en": "🎯 Matched because:", "ru": "🎯 Почему проект вам подходит:", "ar": "طابق لأن:"},
    "card_ai_summary": {"en": "🤖 AI Summary:", "ru": "🤖 Краткое резюме от ИИ:", "ar": "ملخص الذكاء الاصطناعي:"},
    "card_default_match_reason": {
        "en": "Matched your saved filters.",
        "ru": "Проект совпал с вашими фильтрами поиска.",
        "ar": "طابق المرشحات المحفوظة لديك.",
    },
    "open_project": {"en": "Open Project", "ru": "Открыть проект", "ar": "فتح المشروع"},
    "relevant": {"en": "Relevant", "ru": "Подходит", "ar": "مناسب"},
    "not_relevant": {"en": "Not Relevant", "ru": "Не подходит", "ar": "غير مناسب"},
}


PAYMENT_STATUS_LABELS: Final[dict[str, dict[str, str]]] = {
    "waiting": {"en": "waiting", "ru": "ждёт оплаты"},
    "confirming": {"en": "confirming", "ru": "подтверждается"},
    "confirmed": {"en": "confirmed", "ru": "подтверждён"},
    "sending": {"en": "sending", "ru": "в обработке"},
    "partially_paid": {"en": "partially paid", "ru": "оплачен частично"},
    "finished": {"en": "paid", "ru": "оплачен"},
    "failed": {"en": "failed", "ru": "оплата не прошла"},
    "expired": {"en": "expired", "ru": "ссылка истекла"},
    "refunded": {"en": "refunded", "ru": "возврат оформлен"},
}


def text(language: object, key: str, **kwargs: object) -> str:
    normalized = ui_language_or_default(language)
    template = UI_STRINGS[key][normalized]
    return template.format(**kwargs)


def button(language: object, key: str, **kwargs: object) -> str:
    normalized = ui_language_or_default(language)
    template = BUTTON_STRINGS[key][normalized]
    return template.format(**kwargs)


def normalize_message_language(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized.startswith("ru"):
        return "ru"
    if normalized.startswith("ar"):
        return "ar"
    return "en"


def message(language: object, key: str, **kwargs: object) -> str:
    normalized = normalize_message_language(language)
    template = MESSAGE_STRINGS[key].get(normalized) or MESSAGE_STRINGS[key]["en"]
    return template.format(**kwargs)


def payment_status_label(language: object, status: object) -> str:
    normalized_language = ui_language_or_default(language)
    normalized_status = str(status or "").strip().lower()
    template = PAYMENT_STATUS_LABELS.get(normalized_status)
    if template is None:
        return normalized_status or "unknown"
    return template.get(normalized_language) or template["en"]


def project_filter_label(language: object, field_name: str) -> str:
    return shared_project_filter_label(ui_language_or_default(language), field_name)


def project_filter_example(language: object, field_name: str) -> str:
    return shared_project_filter_example(ui_language_or_default(language), field_name)
