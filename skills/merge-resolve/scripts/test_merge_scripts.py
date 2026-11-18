#!/usr/bin/env python3
"""Самопроверка classify_conflicts.py и check_merge_symbols.py на временном git-репо.

Запуск: python3 scripts/test_merge_scripts.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
fails = []

LINES = ['line1', 'line2', 'line3', 'line4', 'line5', 'line6', 'line7']


def git(*args, check=True):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args],
                          check=check, capture_output=True, text=True).stdout


def write(path, text):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text)


def lines(**changes):
    return ''.join(f'{changes.get(line, line)}\n' for line in LINES)


def commit(msg):
    git('add', '-A')
    git('commit', '-q', '-m', msg)


def run(script, *args):
    r = subprocess.run([sys.executable, str(HERE / script), *args], capture_output=True, text=True)
    return r.stdout + r.stderr


def expect(cond, msg):
    if not cond:
        fails.append(msg)


ENTITY_V1 = 'export enum Mode { A }\nexport const keep = 1;\n'
ENTITY_V2 = 'export enum Mode { A, B }\nexport const keep = 1;\n'
ENTITY_V3 = 'export const keep = 1;\n'

# Сборочная ветка повторяет версии master независимыми коммитами — как снимок
# фичи, принесённый в сборку другим мержем. Для git это конфликт от базы, хотя
# своих правок у сборки в older.ts и entity.ts нет.
with tempfile.TemporaryDirectory() as repo:
    os.chdir(repo)
    git('init', '-q', '-b', 'main')
    write('older.ts', 'export const a = 1;\n')
    write('both.ts', lines())
    write('clash.ts', 'export const x = 1;\n')
    write('entity.ts', ENTITY_V1)
    write('slice/ui/types.ts', 'export type Price = number;\nexport const other = 1;\n')
    write('slice/index.ts', "export type { Price } from './ui/types';\n")
    commit('base')

    git('checkout', '-q', '-b', 'build')
    write('older.ts', 'export const a = 2;\n')
    write('both.ts', lines(line1='line1 shared'))
    commit('build takes feature')
    write('both.ts', lines(line1='line1 shared', line2='line2 build'))
    write('clash.ts', 'export const x = 4;\n')
    write('entity.ts', ENTITY_V2)
    write('consumer.ts', "import { Mode } from './entity';\n")
    write('price-consumer.ts', "import { type Price } from './slice';\n")
    commit('build snapshot')

    git('checkout', '-q', 'main')
    write('older.ts', 'export const a = 2;\n')
    write('both.ts', lines(line1='line1 shared'))
    write('entity.ts', ENTITY_V2)
    commit('feature lands')
    write('older.ts', 'export const a = 3;\n')
    write('both.ts', lines(line1='line1 shared', line7='line7 theirs'))
    write('clash.ts', 'export const x = 3;\n')
    write('entity.ts', ENTITY_V3)
    # Price переезжает внутри слайса: индекс реэкспортирует его из нового файла.
    write('slice/ui/types.ts', 'export const other = 1;\n')
    write('slice/model/price.ts', 'export type Price = number;\n')
    write('slice/index.ts', "export type { Price } from './model/price';\n")
    commit('master moves on')

    git('checkout', '-q', 'build')
    git('merge', '-q', 'main', check=False)

    out = run('classify_conflicts.py', '--out', os.path.join(repo, '.out'))
    lists = {name: Path(repo, '.out', f'{name}.txt').read_text().split()
             for name in ('take-theirs', 'take-ours', 'realbase-clean', 'manual')}
    expect(lists['take-theirs'] == ['entity.ts', 'older.ts'], f'take-theirs\n{out}')
    expect(lists['realbase-clean'] == ['both.ts'], f'realbase-clean\n{out}')
    expect(lists['manual'] == ['clash.ts'], f'manual\n{out}')
    expect(Path(repo, '.out/drafts/both.ts').read_text()
           == lines(line1='line1 shared', line2='line2 build', line7='line7 theirs'),
           'черновик both.ts')
    expect('Mode — убран из entity.ts [take-theirs]' in out and 'consumer.ts' in out,
           f'удалённый экспорт Mode с потребителем consumer.ts\n{out}')
    expect('Price' not in out, f'Price по-прежнему реэкспортирует slice/index.ts\n{out}')

    git('merge', '--abort')

# Откат: master откатывает фичу, а сборка поверх фичи правила тот же файл.
# Версия theirs (до фичи) есть в истории ours, но take-ours здесь оставил бы
# фичу в файле, а take-theirs выкинул бы правку сборки.
FEAT_TAIL = 'export const feat = () => {};\n'
with tempfile.TemporaryDirectory() as repo:
    os.chdir(repo)
    git('init', '-q', '-b', 'main')
    write('f.ts', lines())
    commit('base')
    write('f.ts', lines(line1='feat') + FEAT_TAIL)
    commit('feature')

    git('checkout', '-q', '-b', 'build')
    write('f.ts', lines(line1='feat build') + FEAT_TAIL)
    commit('build edits the feature')

    git('checkout', '-q', 'main')
    git('revert', '--no-edit', 'HEAD')
    revert_sha = git('rev-parse', 'HEAD').strip()

    git('checkout', '-q', 'build')
    git('merge', '-q', 'main', check=False)

    out = run('classify_conflicts.py', '--out', os.path.join(repo, '.out'))
    lists = {name: Path(repo, '.out', f'{name}.txt').read_text().split()
             for name in ('take-ours', 'manual')}
    expect(lists['take-ours'] == [] and lists['manual'] == ['f.ts'], f'откат — manual\n{out}')
    expect(f'theirs {revert_sha[:8]} «Revert "feature"» — файлов: 1, в конфликте: 1' in out,
           f'блок ОТКАТЫ\n{out}')
    expect(f'черновик принимает откат (база {revert_sha[:8]}^)' in out, f'причина manual\n{out}')
    draft_path = Path(repo, '.out/drafts/f.ts')
    draft = draft_path.read_text() if draft_path.exists() else ''
    expect('feat = ' not in draft and '||||||| ' in draft,
           f'черновик от версии до отката: хвост фичи снят, line1 в конфликте\n{draft}')

    out = run('check_merge_symbols.py')
    expect(f'ПРОПАЛ   feat — есть в ours, в результате нет; менял коммит theirs {revert_sha[:8]}'
           in out, f'пропажа feat с коммитом отката\n{out}')

    git('merge', '--abort')

# check_merge_symbols: многострочные сигнатуры и файл-перемотка. Оба файла меняют
# обе стороны, иначе merge_scope их отсеет и проверять будет нечего.
A_TAIL = '// t1\n// t2\n'
B_STAY = 'export const stay = 1;\n// t1\n// t2\n'
with tempfile.TemporaryDirectory() as repo:
    os.chdir(repo)
    git('init', '-q', '-b', 'main')
    write('a.ts', 'class A {\n  _toggle(name: string) {}\n}\n'
                  'export const enter = async (x: number) => {};\n' + A_TAIL)
    write('b.ts', 'export const gone = () => {};\n' + B_STAY)
    commit('base')

    git('checkout', '-q', '-b', 'side')
    write('a.ts', 'class A {\n  _toggle(\n    name: string,\n  ): void {}\n}\n'
                  'export const enter = async (\n  x: number,\n): Promise<void> => {};\n' + A_TAIL)
    write('b.ts', 'export const gone = () => {};\n' + B_STAY + '// v1\n')
    commit('reformat, take b v1')

    git('checkout', '-q', 'main')
    write('b.ts', 'export const gone = () => {};\n' + B_STAY + '// v1\n')
    commit('take b v1')
    write('a.ts', Path('a.ts').read_text() + 'export const z = 1;\n')
    write('b.ts', B_STAY + '// v1\n')
    commit('remove gone')

    git('checkout', '-q', 'side')
    git('merge', '-q', '-m', 'merge main', 'main')

    out = run('check_merge_symbols.py')
    expect('СТРУКТУРНАЯ СВЕРКА СИМВОЛОВ (2 файлов)' in out,
           f'обе правленые с двух сторон файла в скоупе\n{out}')
    expect('_toggle' not in out and 'enter' not in out,
           f'многострочные сигнатуры не считаются пропавшими\n{out}')
    expect('gone' not in out, f'пропажа в файле-перемотке не показывается\n{out}')

if fails:
    print('FAIL:\n' + '\n---\n'.join(fails))
    sys.exit(1)
print('OK')
