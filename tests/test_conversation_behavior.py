import copy
import json
import math
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from PIL import Image

from xigpet import pet_core
import test_pet_core as core_tests
from xigpet.chat_text import split_bubbles, limit_sentences
from scripts.media_tools import attach_media, split_media
from xigpet.memory_recall import important_excerpt, prepare_recall
from xigpet.pet_behavior import PetBehavior, FLOAT_RADIUS_X, FLOAT_RADIUS_Y


def media_call(filename="happy.png", purpose="reaction"):
    return {"tool_calls": [{"id": "media", "type": "function", "function": {
        "name": "send_media", "arguments": json.dumps({"filename": filename, "purpose": purpose}),
    }}]}


class ConversationBehaviorTests(unittest.TestCase):
    setUp = core_tests.PetCoreTests.setUp

    def image(self, name="happy.png"):
        Image.new("RGB", (32, 32), "blue").save(pet_core.MEDIA_DIR / name)

    def test_birthday_in_important_memory_is_in_first_request_without_tool_opt_in(self):
        pet_core.IMPORTANT.write_text("- 测试宠物的生日是11月23日。", encoding="utf-8")
        requests = []
        def api(messages, *_args, **kwargs):
            requests.append(copy.deepcopy(messages))
            return core_tests.continuation() if "tools_override" in kwargs else {"content": "我的生日是11月23日呀。"}
        with patch.object(pet_core, "call_deepseek", side_effect=api):
            answer = pet_core.reply("你的生日是几月几号？")
        self.assertIn("11月23日", json.dumps(requests[0], ensure_ascii=False))
        self.assertEqual(answer, "我的生日是11月23日呀。")
        self.assertEqual(len(requests), 2)

    def test_large_important_memory_finds_older_middle_fact_not_only_tail(self):
        content = "- 一条没有相关主题的日常记录。\n" * 700 + "## 测试宠物档案\n- 测试宠物生日11月23日。\n" + "- 另一些无关小事。\n" * 1800
        pet_core.IMPORTANT.write_text(content, encoding="utf-8")
        self.assertNotIn("11月23日", content[-16000:])
        excerpt, relevant = important_excerpt(pet_core.IMPORTANT, "你是哪天出生的？")
        self.assertTrue(relevant)
        self.assertIn("11月23日", excerpt)
        self.assertIn("## 测试宠物档案", excerpt)
        self.assertLessEqual(len(excerpt), 6000)
        self.assertIn("11月23日", pet_core.execute_tool("search_important_memory", {"query": "生日"}))

    def test_old_history_fact_is_read_by_offset_when_important_memory_is_empty(self):
        line = pet_core.append_history("user", "我的生日是11月23日，你要记住。")
        pet_core.append_history("assistant", "好，我记着了。")
        current = pet_core.append_history("user", "你还记得我的生日吗？")
        recall = prepare_recall(pet_core.IMPORTANT, pet_core.memory_catalog(), "我的生日是哪天", before_line=current)
        self.assertTrue(recall.relevant)
        self.assertTrue(any(row["line"] == line and "11月23日" in row["content"] for row in recall.history))
        self.assertTrue(all(row["line"] < current for row in recall.history))

    def test_unknown_draft_is_checked_again_before_it_can_be_sent(self):
        original = "- 测试宠物的生日是11月23日。"
        pet_core.IMPORTANT.write_text(original, encoding="utf-8")
        responses = [{"content": "我不记得自己的生日了。"}, {"content": "我的生日是11月23日。"}, core_tests.continuation()]
        sent = []
        with patch.object(pet_core, "call_deepseek", side_effect=responses) as api:
            answer = pet_core.reply("你的生日是哪天？", on_reply=sent.append)
        self.assertEqual(api.call_count, 3)
        self.assertEqual(sent, ["我的生日是11月23日。"])
        self.assertNotIn("不记得", answer)
        self.assertNotIn("不记得", pet_core.HISTORY.read_text(encoding="utf-8"))
        self.assertEqual(pet_core.IMPORTANT.read_text(encoding="utf-8"), original)

    def test_missing_memory_does_not_fabricate_answer_or_loop(self):
        with patch.object(pet_core, "call_deepseek", side_effect=[{"content": "这件事我还没找到记录，可以再告诉我吗？"}, core_tests.continuation()]) as api:
            answer = pet_core.reply("你的生日是哪天？")
        self.assertIn("没找到记录", answer)
        self.assertEqual(api.call_count, 2)

    def test_memory_lookup_does_not_force_a_long_form_answer(self):
        pet_core.IMPORTANT.write_text("- 生日11月23日。", encoding="utf-8")
        read = {"tool_calls": [{"id": "memory", "type": "function", "function": {
            "name": "search_important_memory", "arguments": '{"query":"生日"}',
        }}]}
        sent = []
        with patch.object(pet_core, "call_deepseek", side_effect=[read, {"content": "第一句。第二句。第三句。第四句。第五句。"}, core_tests.continuation()]):
            pet_core.reply("生日？", on_reply=sent.append)
        self.assertEqual(len(sent), 2)
        self.assertTrue(all(not limit_sentences(content)[1] for content in sent))

    def test_short_blank_line_fragments_merge_but_story_and_links_are_preserved(self):
        self.assertEqual(split_bubbles("好呀。\n\n记住啦。\n\n就这么定了。"), ["好呀。记住啦。就这么定了。"])
        self.assertEqual(split_bubbles("Hello.\n\nHow are you?"), ["Hello. How are you?"])
        link = "https://example.com/path\n\n这是来源。"
        self.assertEqual(split_bubbles(link), link.split("\n\n"))
        story = "\n\n".join(f"第{i}段，" + "小水母游过一片蓝色的海洋，发现了新的星光。" * 3 for i in range(4))
        self.assertGreater(len(split_bubbles(story)), 1)
        self.assertEqual("".join(split_bubbles(story)), story.replace("\n\n", ""))

    def test_brief_or_completed_requests_do_not_continue_even_when_focus_is_present(self):
        for scope, complete in (("brief", False), ("chat", False), ("extended", True)):
            with self.subTest(scope=scope, complete=complete):
                judge = core_tests.continuation(True, "新聊一件事")
                args = json.loads(judge["tool_calls"][0]["function"]["arguments"])
                args.update(response_scope=scope, request_complete=complete)
                judge["tool_calls"][0]["function"]["arguments"] = json.dumps(args)
                with patch.object(pet_core, "call_deepseek", side_effect=[{"content": "好呀。"}, judge]) as api:
                    answer = pet_core.reply("可以吗？")
                self.assertEqual(answer, "好呀。")
                self.assertEqual(api.call_count, 2)

    def test_unfinished_story_can_continue_with_an_explicit_unanswered_part(self):
        sent = []
        with patch.object(pet_core, "call_deepseek", side_effect=[{"content": "从前，有一只小水母。"},
                core_tests.continuation(True, "讲完用户要求的故事结局"), {"content": "后来，她找到了自己的星星。"}, core_tests.continuation()]):
            pet_core.reply("给我讲个有结尾的故事。", on_reply=sent.append)
        self.assertEqual(sent, ["从前，有一只小水母。", "后来，她找到了自己的星星。"])

    def test_autonomous_stickers_have_a_plain_text_exchange_between_them(self):
        self.image()
        responses = [media_call(), {"content": "真开心呀。"}, core_tests.continuation(),
                     media_call(), {"content": "听你这么说我也高兴。"}, core_tests.continuation(),
                     media_call(), {"content": "那就一起庆祝。"}, core_tests.continuation()]
        with patch.object(pet_core, "call_deepseek", side_effect=responses):
            first = pet_core.reply("好开心呀")
            second = pet_core.reply("哈哈真有意思")
            third = pet_core.reply("我们一起庆祝吧")
        self.assertEqual(len(split_media(first)[1]), 1)
        self.assertEqual(split_media(second)[1], [])
        self.assertEqual(len(split_media(third)[1]), 1)

    def test_multiple_unsolicited_tool_calls_cannot_send_a_sticker_per_sentence(self):
        self.image("one.png")
        self.image("two.png")
        calls = media_call("one.png")["tool_calls"] + media_call("two.png")["tool_calls"]
        with patch.object(pet_core, "call_deepseek", side_effect=[{"tool_calls": calls}, {"content": "开心一下。"}, core_tests.continuation()]):
            answer = pet_core.reply("嘿嘿")
        self.assertEqual(len(split_media(answer)[1]), 1)

    def test_explicitly_requested_picture_and_drawing_bypass_reaction_cooldown(self):
        self.image()
        pet_core.append_history("assistant", attach_media("", ["happy.png"]))
        with patch.object(pet_core, "call_deepseek", side_effect=[media_call(purpose="requested"), {"content": "给你。"}, core_tests.continuation()]):
            answer = pet_core.reply("再发一个开心表情给我。")
        self.assertEqual(split_media(answer)[1], ["happy.png"])
        with patch.object(pet_core, "call_deepseek", side_effect=[core_tests.PetCoreTests._drawing_call(self), {"content": "我画了一朵花。"}, core_tests.continuation()]):
            answer = pet_core.reply("再给我画一幅画。")
        self.assertTrue(any(name.startswith("drawings/drawing_") for name in split_media(answer)[1]))

    def test_reaction_cooldown_expires_after_a_long_quiet_interval(self):
        self.image()
        pet_core.memory_catalog().append("assistant", attach_media("", ["happy.png"]),
                                         (datetime.now().astimezone() - timedelta(minutes=15)).isoformat())
        pet_core.append_history("user", "隔了一会回来啦")
        self.assertTrue(pet_core._allow_unsolicited_reaction())

    def test_qq_future_archived_messages_do_not_bypass_reaction_cooldown(self):
        self.image()
        pet_core.append_history("assistant", attach_media("", ["happy.png"]))
        first = pet_core.append_history("user", "当前QQ消息")
        pet_core.append_history("user", "后面的QQ消息")
        dialogue = [{"role": "assistant", "content": attach_media("", ["happy.png"])}, {"role": "user", "content": "当前QQ消息"}]
        self.assertFalse(pet_core._allow_unsolicited_reaction(dialogue))


class FloatVisibilityTests(unittest.TestCase):
    def test_movement_is_visible_and_still_bounded_near_its_anchor(self):
        import random
        behavior = PetBehavior(random.Random(13))
        behavior.state = "float"
        behavior.steps_remaining = 4
        behavior._pick_target()
        self.assertGreaterEqual(math.hypot(*behavior.target), 24)
        farthest = 0
        for _ in range(800):
            behavior.advance(.05)
            farthest = max(farthest, math.hypot(behavior.x, behavior.y))
            self.assertLessEqual(abs(behavior.x), FLOAT_RADIUS_X)
            self.assertLessEqual(abs(behavior.y), FLOAT_RADIUS_Y)
            if behavior.state == "relax":
                break
        self.assertGreater(farthest, 24)
        self.assertEqual(behavior.state, "relax")


if __name__ == "__main__":
    unittest.main()
