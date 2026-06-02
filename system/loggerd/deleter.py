#!/usr/bin/env python3
import os
import time
import shutil
import threading
from pathlib import Path
from openpilot.system.hardware.hw import Paths
from openpilot.common.swaglog import cloudlog
from openpilot.system.loggerd.config import get_available_bytes, get_available_percent
from openpilot.system.loggerd.uploader import listdir_by_creation
from openpilot.system.loggerd.xattr_cache import getxattr

MIN_BYTES = 5 * 1024 * 1024 * 1024
MIN_PERCENT = 10
MAX_RECORDING_AGE_DAYS = 2
MAX_RECORDING_BYTES = 10 * 1024 * 1024 * 1024

DELETE_LAST = ['boot', 'crash']

PRESERVE_ATTR_NAME = 'user.preserve'
PRESERVE_ATTR_VALUE = b'1'
PRESERVE_COUNT = 5


def segment_mtime(path: str) -> float:
  try:
    return os.path.getmtime(path)
  except OSError:
    return time.time()


def is_segment_dir(d: str) -> bool:
  date_str, _, seg_str = d.rpartition("--")
  if not date_str:
    return False
  try:
    int(seg_str)
  except ValueError:
    return False
  return True


def get_dir_size(path: str) -> int:
  total = 0
  for root, _, files in os.walk(path):
    for f in files:
      try:
        total += os.path.getsize(os.path.join(root, f))
      except OSError:
        pass
  return total


def get_recording_dirs_by_creation(dirs: list[str]) -> list[str]:
  return [d for d in dirs if is_segment_dir(d)]


def has_preserve_xattr(d: str) -> bool:
  return getxattr(os.path.join(Paths.log_root(), d), PRESERVE_ATTR_NAME) == PRESERVE_ATTR_VALUE


def get_preserved_segments(dirs_by_creation: list[str]) -> set[str]:
  # skip deleting most recent N preserved segments (and their prior segment)
  preserved = set()
  for n, d in enumerate(filter(has_preserve_xattr, reversed(dirs_by_creation))):
    if n == PRESERVE_COUNT:
      break
    date_str, _, seg_str = d.rpartition("--")

    # ignore non-segment directories
    if not date_str:
      continue
    try:
      seg_num = int(seg_str)
    except ValueError:
      continue

    # preserve segment and two prior
    for _seg_num in range(max(0, seg_num - 2), seg_num + 1):
      preserved.add(f"{date_str}--{_seg_num}")

  return preserved


def deleter_thread(exit_event: threading.Event):
  while not exit_event.is_set():
    dirs = listdir_by_creation(Paths.log_root())
    max_recording_age = time.time() - MAX_RECORDING_AGE_DAYS * 24 * 60 * 60
    recording_dirs = get_recording_dirs_by_creation(dirs)
    recording_size = sum(get_dir_size(os.path.join(Paths.log_root(), d)) for d in recording_dirs)
    deleted_old_segment = False

    for delete_dir in recording_dirs:
      delete_path = os.path.join(Paths.log_root(), delete_dir)
      if segment_mtime(delete_path) >= max_recording_age and recording_size <= MAX_RECORDING_BYTES:
        continue
      if any(name.endswith(".lock") for name in os.listdir(delete_path)):
        continue

      try:
        reason = f"older than {MAX_RECORDING_AGE_DAYS} days" if segment_mtime(delete_path) < max_recording_age else f"recordings over {MAX_RECORDING_BYTES // 1024 ** 3} GB"
        cloudlog.info(f"deleting {delete_path}: {reason}")
        shutil.rmtree(delete_path)
        deleted_old_segment = True
        break
      except OSError:
        cloudlog.exception(f"issue deleting {delete_path}")

    if deleted_old_segment:
      exit_event.wait(.1)
      continue

    out_of_bytes = get_available_bytes(default=MIN_BYTES + 1) < MIN_BYTES
    out_of_percent = get_available_percent(default=MIN_PERCENT + 1) < MIN_PERCENT

    if out_of_percent or out_of_bytes:
      preserved_dirs = get_preserved_segments(dirs)

      # remove the earliest directory we can
      for delete_dir in sorted(dirs, key=lambda d: (d in DELETE_LAST, d in preserved_dirs)):
        delete_path = os.path.join(Paths.log_root(), delete_dir)

        if any(name.endswith(".lock") for name in os.listdir(delete_path)):
          continue

        if Path(Paths.log_root_external()).is_mount():
          out_of_bytes_external = get_available_bytes(default=MIN_BYTES + 1, path_type="external") < MIN_BYTES
          out_of_percent_external = get_available_percent(default=MIN_PERCENT + 1, path_type="external") < MIN_PERCENT

          if out_of_percent_external or out_of_bytes_external:
            dirs_external = listdir_by_creation(Paths.log_root_external())

            # remove the earliest external directory we can
            for delete_dir_external in sorted(dirs_external):
              delete_path_external = os.path.join(Paths.log_root_external(), delete_dir_external)
              try:
                cloudlog.warning(f"deleting {delete_path_external}")
                shutil.rmtree(delete_path_external)
                break
              except OSError:
                cloudlog.exception(f"issue deleting {delete_path_external}")

          # move directory from internal to external
          path_external = os.path.join(Paths.log_root_external(), delete_dir)
          try:
            cloudlog.warning(f"moving {delete_path} to {path_external}")
            start = time.monotonic()
            shutil.move(delete_path, path_external)
            cloudlog.warning(f"moved {delete_path} to {path_external} in {time.monotonic() - start:.2f}s")
            break
          except Exception:
            cloudlog.error(f"issue moving {delete_path} to {path_external}")
            try:
              cloudlog.warning(f"deleting {delete_path}")
              shutil.rmtree(delete_path)
              break
            except OSError:
              cloudlog.exception(f"issue deleting {delete_path}")
          continue

        try:
          cloudlog.info(f"deleting {delete_path}")
          shutil.rmtree(delete_path)
          break
        except OSError:
          cloudlog.exception(f"issue deleting {delete_path}")
      exit_event.wait(.1)
    else:
      exit_event.wait(30)


def main():
  deleter_thread(threading.Event())


if __name__ == "__main__":
  main()
