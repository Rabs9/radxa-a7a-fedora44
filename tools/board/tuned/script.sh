#!/bin/sh
# script.sh of the a7a-* tuned profiles: KDE's power slider -> tuned-ppd -> tuned -> this -> a7a-clock power <mode>.
# The mode comes from the profile's directory name, so the three profiles share this file. A failure must not keep
# tuned from switching: it is logged by tuned, and the clocks stay as they were.
case "$1" in
  start)
    case "$(basename "$(dirname "$(readlink -f "$0")")")" in
      a7a-powersave)   m=power-saver ;;
      a7a-balanced)    m=balanced ;;
      a7a-performance) m=performance ;;
      *) exit 0 ;;
    esac
    /usr/local/bin/a7a-clock power "$m" || echo "a7a-clock power $m failed" >&2 ;;
esac
exit 0
