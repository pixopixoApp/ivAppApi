from app.interaction_inventory import interaction_filter_keys


def test_interaction_filter_keys_match_expert_editor_values():
    timeline = {
        "interactions": [
            {"gesture": "tap"},
            {
                "gesture": "camera_motion",
                "vision": {"target": "face_smile"},
            },
            {"gesture": "pinch", "pinch_direction": "outward"},
            {"gesture": "draw_circle", "rotation_direction": "clockwise"},
            {"gesture": "mic_level_continuous"},
        ]
    }

    assert interaction_filter_keys(timeline) == sorted(
        {
            "tap",
            "camera_motion",
            "camera_motion::face_smile",
            "pinch",
            "pinch::outward",
            "draw_circle",
            "draw_circle::clockwise",
            "mic_continuous",
            "mic_continuous::voice_pitch",
        }
    )


def test_interaction_filter_keys_walk_story_clips_and_deduplicate_videos():
    story = {
        "clips": {
            "A": {"timeline": {"interactions": [{"gesture": "double_tap"}]}},
            "B": {
                "timeline": {
                    "interactions": [
                        {"gesture": "double_tap"},
                        {"gesture": "camera_continuous"},
                    ]
                }
            },
        }
    }

    assert interaction_filter_keys(story) == [
        "camera_continuous",
        "camera_continuous::hand_finger_snap",
        "double_tap",
    ]
