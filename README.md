# Player Heads для Endstone

За убийство игрока выпадает его голова. Её можно поставить или надеть.

## Видео

[![Смотреть демонстрацию Player Heads](https://i.ytimg.com/vi/rHFu__9gN-M/hqdefault.jpg)](https://youtu.be/rHFu__9gN-M)

## Установка

1. Останови сервер и положи [Player Heads 1.3.0](https://github.com/HotTabGit/Player-Head-Endstone/releases/download/v1.3.0/endstone_player_heads-1.3.0-py3-none-any.whl) в `plugins`.
2. При обновлении убери прежний `.whl` и сохрани `plugins/player_heads`.
3. Запусти сервер и прими ресурспак при входе.

Проверенная связка: Endstone 0.11.15, Bedrock 26.60 (протокол 2225), `online-mode=true`. Для Pterodactyl используй [egg Endstone](https://github.com/EndstoneMC/pterodactyl).

Быстрые скины поставленных голов включены по умолчанию. Для новых скинов надетых голов нужен перезапуск с обновлённым ресурспаком. На новом сервере пак создаётся автоматически. Если при первом запуске папки мира ещё не было, перезапусти сервер ещё раз.

<img width="160" height="169" alt="Голова игрока" src="https://github.com/user-attachments/assets/9fe672c3-d135-4dd6-9d56-288320f3ff81" />

## Команды

- `/heads status` показывает состояние.
- `/heads give [ник]` выдаёт голову онлайн-игрока. Без ника выдаёт свою.
- `/heads reload` перечитывает настройки.

Настройки в `plugins/player_heads/config.toml`. Право `player_heads.admin` по умолчанию у операторов.

Лицензия MIT.
