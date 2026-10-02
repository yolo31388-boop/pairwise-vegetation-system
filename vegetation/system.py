"""
pairwise-vegetation-system - VegetationSystem

This module contains a deliberately broken implementation.
Fix all bugs so that tests pass.
"""


class VegetationSystem:
    def __init__(self):
        self.state = {}

    def render_grass(self, *args, **kwargs):
        """BUG: 草地渲染不做交互弯曲，角色走过草地草不动"""
        # Intentionally broken implementation
        return None

    def check_grass_collision(self, *args, **kwargs):
        """BUG: 草地碰撞只检查中心点，不检查整个角色体积"""
        # Intentionally broken implementation
        return None

    def distribute_vegetation(self, *args, **kwargs):
        """BUG: 植被分布用随机数，不考虑地形坡度和高度"""
        # Intentionally broken implementation
        return None

    def animate_vegetation(self, *args, **kwargs):
        """BUG: 植被动画只做整体摆动，不做逐片草的相位差"""
        # Intentionally broken implementation
        return None

    def cull_vegetation(self, *args, **kwargs):
        """BUG: 植被剔除只检查视锥，不检查遮挡"""
        # Intentionally broken implementation
        return None

