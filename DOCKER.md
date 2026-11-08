# NavigatorMAX: образ с базой и токенами внутри

Образ включает проект, rostov.json и configuration/config.txt. После сборки отдельные файлы и настройки для запуска не нужны. Любой, кто скачает образ, сможет извлечь встроенные токены.

## 1. Подготовка

Скопируйте Dockerfile и .dockerignore из этого архива в папку с bot.py, заменив прежние версии этих двух файлов. Если Проводник спросит о замене файлов — подтвердите замену. Файл DOCKER.md содержит эту инструкцию.

Убедитесь, что рядом с bot.py находятся requirements.txt, настоящий rostov.json и папка configuration с заполненным config.txt. Рабочий Python-код менять не нужно.

Названия Dockerfile и .dockerignore должны быть без дополнительного расширения .txt.

## 2. PowerShell

Запустите Docker Desktop и дождитесь готовности движка. В Проводнике откройте папку проекта. В адресной строке напишите powershell и нажмите Enter.

Проверьте Docker:

```powershell
docker version
```

Должны отображаться Client и Server. Если Server недоступен, дождитесь запуска Docker Desktop. Используйте режим Linux-контейнеров.

## 3. Сборка

```powershell
docker build -t navigatormax:demo .
```

Точка в конце обязательна. Дождитесь завершения команды. При первой сборке загружаются Python и зависимости; база тоже копируется в образ. Записи в .gitignore не мешают Docker скопировать базу и конфигурацию.

## 4. Запуск

Если этот MAX-бот сейчас работает через python bot.py, остановите его. Затем:

```powershell
docker run -d --init --name navigatormax --restart unless-stopped navigatormax:demo
docker logs -f navigatormax
```

Проверьте бота в MAX. Ctrl+C завершает просмотр журнала; контейнер продолжает работать. Порты открывать не требуется.

Если имя navigatormax уже занято контейнером из предыдущей инструкции, остановите и удалите старый контейнер, затем повторите команду docker run:

```powershell
docker stop navigatormax
docker rm navigatormax
```

Команды управления созданным контейнером:

```powershell
docker stop navigatormax
docker start navigatormax
docker logs --tail 100 navigatormax
```

## 5. Публикация в Docker Hub

Откройте https://hub.docker.com/ и войдите в свой аккаунт. Выберите My Hub → Repositories → Create repository. В Namespace выберите свой аккаунт, Repository Name задайте navigatormax. Если нужен общедоступный образ, выберите Public. Нажмите Create.

В той же консоли PowerShell:

```powershell
docker login
$navigatorDockerUser = Read-Host "Введи свой логин Docker Hub"
docker tag navigatormax:demo "$navigatorDockerUser/navigatormax:demo"
docker push "$navigatorDockerUser/navigatormax:demo"
```

Введите логин Docker Hub, а не адрес электронной почты. При необходимости docker login предложит вход через браузер. Дождитесь завершения push. В Docker Hub на вкладке Tags появится demo.

Ссылка имеет вид https://hub.docker.com/r/ВАШ_ЛОГИН/navigatormax. Подставьте свой логин и добавьте ссылку в README GitHub-репозитория.

## 6. Запуск на другом компьютере

Нужен только установленный и работающий Docker. Откройте PowerShell, укажите логин владельца опубликованного образа:

```powershell
$navigatorDockerUser = Read-Host "Введи логин автора образа в Docker Hub"
docker run -d --init --name navigatormax --restart unless-stopped "$navigatorDockerUser/navigatormax:demo"
```

Если образа ещё нет на компьютере, Docker скачает его. База и токены уже внутри. Это тот же MAX-бот с теми же реквизитами: остановите его на прежнем компьютере перед переносом. Обычная сборка на ПК Intel/AMD рассчитана на такую же архитектуру; для Raspberry Pi нужна отдельная ARM-сборка.

## 7. GitHub

Docker Hub хранит готовый образ, GitHub — Dockerfile и инструкцию. В README добавьте строку, заменив YOUR_DOCKERHUB_USERNAME:

```markdown
Docker-образ: [Docker Hub](https://hub.docker.com/r/YOUR_DOCKERHUB_USERNAME/navigatormax).

Запуск: [инструкция Docker](DOCKER.md).
```

В GitHub достаточно отправить Dockerfile, .dockerignore, DOCKER.md и обновлённый README. Файлы rostov.json и configuration/config.txt копируются в образ с вашего ПК; добавлять их в Git не требуется.

```powershell
git add Dockerfile .dockerignore DOCKER.md README.md
git commit -m "Add Docker container with bundled database and configuration"
git push
```

## Обновление

При изменении базы, кода или токенов снова выполните docker build, остановите и удалите старый контейнер и создайте новый через docker run. Если распространяете образ через Docker Hub, повторите tag и push.

Docker Hub хранит образ, но не запускает бота. Компьютер или сервер с контейнером должен быть включён. На Windows должен работать Docker Desktop; настройка restart действует при работающем движке Docker.
