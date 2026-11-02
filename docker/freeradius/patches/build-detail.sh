#!/bin/sh
set -eu

# Keep the native module ABI and its corresponding GPL source pinned together.
test "$(dpkg-query -W -f='${Version}' freeradius)" = '3.2.1+dfsg-4+deb12u1'
mkdir -p /build/detail/aux
cd /build/detail
curl -fsSL --retry 2 'https://deb.debian.org/debian/pool/main/f/freeradius/freeradius_3.2.1+dfsg.orig.tar.xz' -o source.tar.xz
echo 'f28a7c4dc0515d24b323c157a46c7d0f0fa7cdc76bf25e4044251d33d9d52832  source.tar.xz' | sha256sum -c -
tar -xf source.tar.xz
curl -fsSL --retry 2 'https://sources.debian.org/data/main/f/freeradius/3.2.1%2Bdfsg-4%2Bdeb12u1/src/modules/rlm_detail/rlm_detail.c' -o rlm_detail.c
echo '43d2574c5869ac80d93ac3b1f2eead5cc0dc1e04307f3f1ae3b95a45e78db10e  rlm_detail.c' | sha256sum -c -
patch --fuzz=0 < /detail-close.patch
# Debian's development package omits a few internal module headers. Prefer its
# installed ABI headers, falling back only to the pinned upstream headers.
ln -s /usr/include/freeradius /usr/include/freeradius-devel
ln -s /build/detail/freeradius-server-3.2.1/src/include aux/freeradius-devel
ln -s /build/detail/freeradius-server-3.2.1/src/include aux/freeradius
gcc -shared -fPIC -O2 -fstack-protector-strong -D_FORTIFY_SOURCE=2 \
    -Wformat -Werror=format-security -Wl,-z,relro,-z,now \
    -I/usr/include -I/build/detail/aux \
    -include freeradius-devel/autoconf.h -include freeradius-devel/build.h \
    rlm_detail.c -L/usr/lib/freeradius -lfreeradius-radius -lfreeradius-server \
    -o rlm_detail.so
