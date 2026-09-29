# Подключение на Linux

## Установка клиента

- [NekoRay/NekoBox](https://github.com/MatsuriDayo/nekoray/releases) — GUI
- [sing-box](https://sing-box.sagernet.org/) — CLI
- [hiddify-next](https://github.com/hiddify/hiddify-next/releases)

## NekoRay

1. Распакуйте архив, запустите `nekoray`.
2. Программа → Добавить профиль из буфера обмена (скопировав `vless://...`).
3. Выберите профиль → Запустить (режим TUN потребует root/sudo).

## sing-box (CLI)

Пример конфига:

```json
{
  "outbounds": [
    {
      "type": "vless",
      "server": "ВАШ_HOST",
      "server_port": 443,
      "uuid": "ВАШ_UUID",
      "flow": "xtls-rprx-vision",
      "tls": {
        "enabled": true,
        "server_name": "SNI",
        "utls": { "enabled": true, "fingerprint": "chrome" },
        "reality": { "enabled": true, "public_key": "PBK", "short_id": "SID" }
      }
    }
  ]
}
```

Значения HOST/UUID/SNI/PBK/SID возьмите из ссылки `vless://`.

## Если не работает

- Для TUN нужен root: запускайте через sudo.
- Проверьте, что порт 443 не занят локальным сервисом.
- 🆘 Поддержка.
