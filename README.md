# FB2 to EPUB Converter

Небольшая CLI-программа на Python для конвертации книг `.fb2` в `.epub`.

## Использование

```bash
python3 fb2_to_epub.py book.fb2
```

По умолчанию рядом с исходным файлом появится `book.epub`.

Можно указать путь явно:

```bash
python3 fb2_to_epub.py book.fb2 -o output/book.epub
```

## Что поддерживается

- основные метаданные: название, авторы, язык, идентификатор;
- главы из `body/section`;
- абзацы, заголовки, подзаголовки, цитаты, стихи;
- inline-разметка: emphasis, strong, code, sub, sup, ссылки;
- картинки из `binary`, включая coverpage.

Зависимости не нужны: используется только стандартная библиотека Python.
