from music_migrate import best, norm, organize, score


def test_matching():
    src = {"title": 'Ranvijay\'s Entry Medley (From "Animal")', "artist": "A.R. Rahman", "duration": 218}
    assert norm(src["title"]) == "ranvijay s entry medley"
    good = {"title": "Ranvijay's Entry Medley", "artist": "A.R. Rahman", "duration": 220}
    cover = {"title": "Ranvijay's Entry Medley", "artist": "Some Cover Band", "duration": 218}
    other = {"title": "Kun Faya Kun", "artist": "A.R. Rahman", "duration": 471}
    assert best(src, [cover, other, good])[1] is good
    assert score({"title": "Kun Faya Kun", "artist": "A.R. Rahman, Javed Ali & Mohit Chauhan", "duration": 471},
                 {"title": "Kun Faya Kun", "artist": "Mohit Chauhan, A.R. Rahman", "duration": 471}) == 1.0
    assert score(src, {**good, "duration": 400}) < score(src, good)  # very different length = likely wrong version


def test_organize():
    lib = {"Library": [{"genre": "Pop", "fav": True}, {"genre": "Pop", "fav": False}, {"genre": "Rock", "fav": False}]}
    assert set(organize(lib, 2)) == {"Favorites", "Pop"}
    assert set(organize(lib, 0)) == {"Favorites"}


def test_rejects_covers_and_lookalikes():
    paradise = {"title": "Another Day In Paradise", "artist": "CAT VS CAT & JOYNER"}
    assert score(paradise, {"title": "Another Day In Paradise", "artist": "Mad V"}) < 0.75
    jeene = {"title": "Jeene Ki Tamanna", "artist": "Lata Mangeshkar"}
    assert score(jeene, {"title": "Panna Ki Tamanna", "artist": "Kishore Kumar, Lata Mangeshkar"}) < 0.75
    assert score({"title": "Señorita", "artist": "Farhan Akhtar & Hrithik Roshan"},
                 {"title": "Senorita", "artist": "Farhan Akhtar | Hrithik Roshan"}) == 1.0
    assert score({"title": "4 Men Down", "artist": "Millind Gaba"}, {"title": "4mendown", "artist": "Millind Gaba"}) == 1.0
    assert score({"title": "Sunrise Demo Final Mix", "artist": ""}, {"title": "Sunrise", "artist": "Somebody Else"}) < 0.75
    assert score({"title": "Summer of '69", "artist": "Bryan Adams"},
                 {"title": "Summer of 69 (Classic Version)", "artist": "Bryan Adams"}) == 1.0
    assert score({"title": "Alan Walker - Dreamer [NCS Release]", "artist": ""}, {"title": "Dreamer", "artist": "Alan Walker"}) == 1.0


def test_choose_and_transfer_use_picks():
    from music_migrate import choose, transfer, tkey
    a = {"title": "A", "artist": "X", "fav": True}
    b = {"title": "B", "artist": "Y", "fav": False}
    matches = {tkey(a): (a, [(0.9, {"id": "a1"})]), tkey(b): (b, [(0.5, {"id": "b1"})])}
    ids = choose(matches)
    assert ids == {tkey(a): "a1", tkey(b): None}  # weak match is left out until the user picks it

    class Dest:
        liked, lists = [], {}
        def like(self, ids, progress): self.liked += ids
        def playlist(self, name, ids): self.lists[name] = ids; return "url"

    d = Dest()
    lib = {"Library": [a, b]}
    transfer(d, "apple", lib, {**lib, "Favorites": [a]}, ids)
    assert d.liked == ["a1"] and d.lists == {"Favorites": ["a1"]}


def test_ytm_login_accepts_any_paste():
    from music_migrate import MigrateError, ytm_login
    jar = "SID=a; __Secure-3PAPISID=xyz/abc; HSID=b"
    pastes = [
        f"curl 'https://music.youtube.com/youtubei/v1/browse?prettyPrint=false' \\\n  -H 'accept: */*' \\\n  -b '{jar}' \\\n  -H 'x-goog-authuser: 1' \\\n  --data-raw '{{}}'",
        f"curl 'https://music.youtube.com/youtubei/v1/browse' -X POST -H 'Cookie: {jar}' -H 'X-Goog-AuthUser: 1'",
        f'curl ^"https://music.youtube.com/youtubei/v1/browse^" ^\n  -H ^"accept: */*^" ^\n  -b ^"{jar}^" ^\n  -H ^"x-goog-authuser: 1^"',
        f":authority: music.youtube.com\naccept: */*\ncookie: {jar}\nx-goog-authuser: 1",
        f"accept\n*/*\ncookie\n{jar}\nx-goog-authuser\n1",
    ]
    for p in pastes:
        assert ytm_login(p).splitlines()[:2] == [f"cookie: {jar}", "x-goog-authuser: 1"], p
    assert ytm_login(jar).startswith(f"cookie: {jar}\nx-goog-authuser: 0")
    for bad in ["alt-svc: h3=\":443\"\ncontent-encoding: br", "", "curl 'https://music.youtube.com/' -H 'accept: */*'"]:
        try:
            ytm_login(bad)
            raise AssertionError(bad)
        except MigrateError:
            pass
