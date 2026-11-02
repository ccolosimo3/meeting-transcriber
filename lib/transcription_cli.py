"""Private line-oriented shell presentation seam for shared operations."""
from __future__ import annotations
import os
key_override = os.environ.pop('ASSEMBLYAI_API_KEY', None)
import argparse
from pathlib import Path
import sys
from transcription import select_or_import, recording_at, transcribe, cleanup_published_run, OperationError
from transcription_config import SetupError
from transcribe_assemblyai import cleanup_required_block, format_failure


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='action', required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument('source', type=Path)
    prepare.add_argument('root', type=Path)
    run = commands.add_parser('run')
    run.add_argument('source', type=Path)
    run.add_argument('output_root', type=Path)
    run.add_argument('--speakers', type=int)
    cleanup = commands.add_parser('cleanup')
    cleanup.add_argument('canonical', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            print(select_or_import(args.source, args.root).path)
            return 0
        if args.action == 'cleanup':
            result = cleanup_published_run(args.canonical, key_override=key_override, stderr=sys.stderr)
            return 0 if result.deletion_confirmed else 1
        result = transcribe(recording_at(args.source), args.speakers, key_override=key_override, stderr=sys.stderr, output_root=args.output_root)
        if os.environ.get('MEETING_TRANSCRIBER_RESULT_FILE'):
            from private_files import atomic_write_private
            atomic_write_private(Path(os.environ['MEETING_TRANSCRIBER_RESULT_FILE']), str(result.canonical_path.parent) + '\n')
        print(result.canonical_path)
        if not result.deletion_confirmed:
            for line in cleanup_required_block(args.source, result.canonical_path, None):
                print(line, file=sys.stderr)
        return result.exit_code
    except (OSError, ValueError, RuntimeError) as error:
        if isinstance(error, OperationError) and error.exit_code != 2:
            details = error.details
            if details:
                for line in format_failure(details, args.source):
                    print(line, file=sys.stderr)
            else:
                print(str(error), file=sys.stderr)
            return error.exit_code
        print(f'\nSetup problem\n  Problem     {error}\n  Recording   safe at {getattr(args, "source", "its original location")}\n  No audio left this computer\n  Fix         meeting setup or meeting doctor', file=sys.stderr)
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
